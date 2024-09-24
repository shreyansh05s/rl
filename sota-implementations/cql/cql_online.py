# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""CQL Example.

This is a self-contained example of an online CQL training script.

It works across Gym and MuJoCo over a variety of tasks.

The helper functions are coded in the utils.py associated with this script.

"""
import time

import hydra
import numpy as np
import torch
import tqdm
from tensordict import TensorDict
from tensordict.nn import CudaGraphModule

from torchrl._utils import logger as torchrl_logger, timeit
from torchrl.envs.utils import ExplorationType, set_exploration_type
from torchrl.record.loggers import generate_exp_name, get_logger

from utils import (
    dump_video,
    log_metrics,
    make_collector,
    make_continuous_cql_optimizer,
    make_continuous_loss,
    make_cql_model,
    make_environment,
    make_replay_buffer,
)


@hydra.main(version_base="1.1", config_path="", config_name="online_config")
def main(cfg: "DictConfig"):  # noqa: F821
    # Create logger
    exp_name = generate_exp_name("CQL-online", cfg.logger.exp_name)
    logger = None
    if cfg.logger.backend:
        logger = get_logger(
            logger_type=cfg.logger.backend,
            logger_name="cql_logging",
            experiment_name=exp_name,
            wandb_kwargs={
                "mode": cfg.logger.mode,
                "config": dict(cfg),
                "project": cfg.logger.project_name,
                "group": cfg.logger.group_name,
            },
        )

    # Set seeds
    torch.manual_seed(cfg.env.seed)
    np.random.seed(cfg.env.seed)
    device = cfg.optim.device
    if device in ("", None):
        if torch.cuda.is_available():
            device = "cuda:0"
        else:
            device = "cpu"
    device = torch.device(device)

    # Create env
    train_env, eval_env = make_environment(
        cfg,
        cfg.env.train_num_envs,
        cfg.env.eval_num_envs,
        logger=logger,
    )

    # Create replay buffer
    replay_buffer = make_replay_buffer(
        batch_size=cfg.optim.batch_size,
        prb=cfg.replay_buffer.prb,
        buffer_size=cfg.replay_buffer.size,
        device="cpu",
    )

    # create agent
    model = make_cql_model(cfg, train_env, eval_env, device)

    # Create collector
    collector = make_collector(cfg, train_env, actor_model_explore=model[0])

    # Create loss
    loss_module, target_net_updater = make_continuous_loss(cfg.loss, model)

    # Create optimizer
    (
        policy_optim,
        critic_optim,
        alpha_optim,
        alpha_prime_optim,
    ) = make_continuous_cql_optimizer(cfg, loss_module)
    # Main loop
    start_time = time.time()
    collected_frames = 0
    pbar = tqdm.tqdm(total=cfg.collector.total_frames)

    init_random_frames = cfg.collector.init_random_frames
    num_updates = int(
        cfg.collector.env_per_collector
        * cfg.collector.frames_per_batch
        * cfg.optim.utd_ratio
    )
    prb = cfg.replay_buffer.prb
    frames_per_batch = cfg.collector.frames_per_batch
    evaluation_interval = cfg.logger.log_interval
    eval_rollout_steps = cfg.logger.eval_steps

    def update(sampled_tensordict):

        sampled_tensordict_input = sampled_tensordict.copy()
        critic_optim.zero_grad()
        q_loss, metadata = loss_module.q_loss(sampled_tensordict_input)
        cql_loss, metadata_cql = loss_module.cql_loss(sampled_tensordict_input)
        metadata.update(metadata)
        q_loss = q_loss + cql_loss
        q_loss.backward()
        # torch.nn.utils.clip_grad_norm_(critic_optim.param_groups[0]["params"], 1.0)
        critic_optim.step()

        if loss_module.with_lagrange:
            alpha_prime_optim.zero_grad()
            alpha_prime_loss, metadata_aprime = loss_module.alpha_prime_loss(
                sampled_tensordict_input
            )
            metadata.update(metadata_aprime)
            alpha_prime_loss.backward()
            # torch.nn.utils.clip_grad_norm_(alpha_prime_optim.param_groups[0]["params"], 1.0)
            alpha_prime_optim.step()

        policy_optim.zero_grad()
        # loss_actor_bc, _ = loss_module.actor_bc_loss(sampled_tensordict)
        actor_loss, actor_metadata = loss_module.actor_loss(sampled_tensordict_input)
        metadata.update(actor_metadata)
        actor_loss.backward()
        # torch.nn.utils.clip_grad_norm_(policy_optim.param_groups[0]["params"], 1.0)
        policy_optim.step()

        alpha_optim.zero_grad()
        alpha_loss, metadata_actor = loss_module.alpha_loss(actor_metadata)
        metadata.update(metadata_actor)
        alpha_loss.backward()
        # torch.nn.utils.clip_grad_norm_(alpha_optim.param_groups[0]["params"], 1.0)
        alpha_optim.step()
        loss_td = TensorDict(metadata)

        loss_td["loss_actor"] = actor_loss
        loss_td["loss_qvalue"] = q_loss
        loss_td["loss_cql"] = cql_loss
        loss_td["loss_alpha"] = alpha_loss
        if alpha_prime_optim:
            alpha_prime_loss = loss_td["loss_alpha_prime"]

        loss = actor_loss + alpha_loss + q_loss
        if alpha_prime_optim is not None:
            loss = loss + alpha_prime_loss

        loss_td["loss"] = loss
        return loss_td.detach()

    if cfg.loss.compile:
        update = torch.compile(update, mode=cfg.loss.compile_mode)

    if cfg.loss.cudagraphs:
        update = CudaGraphModule(update, in_keys=[], out_keys=[], warmup=5)

    sampling_start = time.time()
    collector_iter = iter(collector)
    for i in range(cfg.collector.total_frames):
        timeit.print()
        timeit.erase()
        with timeit("collection"):
            tensordict = next(collector_iter)
        sampling_time = time.time() - sampling_start
        pbar.update(tensordict.numel())
        with timeit("update policies"):
            # update weights of the inference policy
            collector.update_policy_weights_()

        tensordict = tensordict.reshape(-1)
        current_frames = tensordict.numel()
        # add to replay buffer
        with timeit("extend"):
            replay_buffer.extend(tensordict.cpu())
        collected_frames += current_frames

        # optimization steps
        training_start = time.time()
        if collected_frames >= init_random_frames:
            log_loss_td = TensorDict({}, [num_updates])
            for j in range(num_updates):
                # sample from replay buffer
                with timeit("sample"):
                    sampled_tensordict = replay_buffer.sample()
                if sampled_tensordict.device != device:
                    sampled_tensordict = sampled_tensordict.to(
                        device, non_blocking=True
                    )
                else:
                    sampled_tensordict = sampled_tensordict.clone()

                with timeit("update"):
                    loss_td = update(sampled_tensordict)
                log_loss_td[j] = loss_td

                with timeit("target net"):
                    # update qnet_target params
                    target_net_updater.step()

                # update priority
                if prb:
                    replay_buffer.update_priority(sampled_tensordict)

        training_time = time.time() - training_start
        episode_rewards = tensordict["next", "episode_reward"][
            tensordict["next", "done"]
        ]
        # Logging
        metrics_to_log = {}
        if len(episode_rewards) > 0:
            episode_length = tensordict["next", "step_count"][
                tensordict["next", "done"]
            ]
            metrics_to_log["train/reward"] = episode_rewards.mean().item()
            metrics_to_log["train/episode_length"] = episode_length.sum().item() / len(
                episode_length
            )
        if collected_frames >= init_random_frames:
            metrics_to_log["train/loss_actor"] = log_loss_td.get("loss_actor").mean()
            metrics_to_log["train/loss_qvalue"] = log_loss_td.get("loss_qvalue").mean()
            metrics_to_log["train/loss_alpha"] = log_loss_td.get("loss_alpha").mean()
            if alpha_prime_optim is not None:
                metrics_to_log["train/loss_alpha_prime"] = log_loss_td.get(
                    "loss_alpha_prime"
                ).mean()
            # metrics_to_log["train/entropy"] = log_loss_td.get("entropy").mean()
            metrics_to_log["train/sampling_time"] = sampling_time
            metrics_to_log["train/training_time"] = training_time

        # Evaluation

        prev_test_frame = ((i - 1) * frames_per_batch) // evaluation_interval
        cur_test_frame = (i * frames_per_batch) // evaluation_interval
        final = current_frames >= collector.total_frames
        if (i >= 1 and (prev_test_frame < cur_test_frame)) or final:
            with set_exploration_type(
                ExplorationType.DETERMINISTIC
            ), torch.no_grad(), timeit("eval"):
                eval_start = time.time()
                eval_rollout = eval_env.rollout(
                    eval_rollout_steps,
                    model[0],
                    auto_cast_to_device=True,
                    break_when_any_done=True,
                )
                eval_time = time.time() - eval_start
                eval_reward = eval_rollout["next", "reward"].sum(-2).mean().item()
                eval_env.apply(dump_video)
                metrics_to_log["eval/reward"] = eval_reward
                metrics_to_log["eval/time"] = eval_time

        log_metrics(logger, metrics_to_log, collected_frames)
        sampling_start = time.time()

    collector.shutdown()
    end_time = time.time()
    execution_time = end_time - start_time
    torchrl_logger.info(f"Training took {execution_time:.2f} seconds to finish")

    collector.shutdown()
    if not eval_env.is_closed:
        eval_env.close()
    if not train_env.is_closed:
        train_env.close()


if __name__ == "__main__":
    main()
