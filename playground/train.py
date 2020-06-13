"""
The main training loop.

The training is usually started using one of the commands in the `scripts` directory.

Usage:
```bash
# run an experiment to train the Walker2D environment (PyBullet)
./scripts/local_run_playground_train.sh  w2_test_experiment  env_name='pybullet_envs:Walker2DBulletEnv-v0'

# run the same experiment with the NET architecture symmetry method (other options include "traj, loss, phase, net2")
./scripts/local_run_playground_train.sh  w2_net_experiment  env_name='pybullet_envs:Walker2DBulletEnv-v0' mirror_method=net
"""
import copy
import multiprocessing
import os
import time
from collections import deque
from glob import glob

current_dir = os.path.dirname(os.path.realpath(__file__))
parent_dir = os.path.dirname(current_dir)
os.sys.path.insert(0, parent_dir)

import numpy as np
import torch

from algorithms.ppo import PPO
from algorithms.storage import RolloutStorage
from common.controller import SoftsignActor, Policy
from common.envs_utils import (
    make_env,
    make_vec_envs,
    cleanup_log_dir,
    get_mirror_function,
)
from common.misc_utils import linear_decay, exponential_decay, set_optimizer_lr
from common.csv_utils import ConsoleCSVLogger
from common.sacred_utils import ex, init, seed_all

from symmetry.consts import MirrorMethods
from symmetry.net import SymmetricNet, SymmetricVNet
from symmetry.net2 import SymmetricNetV2
from symmetry.env_utils import get_env_name_for_method


@ex.config
def configs():
    env_name = "pybullet_envs:Walker2DBulletEnv-v0"

    # mirroring method
    mirror_method = "none"
    assert mirror_method in MirrorMethods

    sym_value_net = (
        mirror_method == MirrorMethods.net or mirror_method == MirrorMethods.net2
    )

    # Auxiliary configurations
    num_frames = 6e7
    seed = 16
    cuda = torch.cuda.is_available()
    save_every = 1e7
    log_interval = 1
    load_saved_controller = False

    # Sampling parameters
    episode_steps = 50000
    num_processes = multiprocessing.cpu_count()
    num_steps = episode_steps // num_processes
    mini_batch_size = 1024
    num_mini_batch = episode_steps // mini_batch_size

    # Algorithm hyper-parameters
    use_gae = True
    lr_decay_type = "exponential"
    robot_power_decay_type = "exponential"
    gamma = 0.99
    gae_lambda = 0.95
    lr = 0.0003

    aux_loss_coef = 4

    ppo_params = {
        "use_clipped_value_loss": False,
        "num_mini_batch": num_mini_batch,
        "entropy_coef": 0.0,
        "symmetry_coef": aux_loss_coef if mirror_method == MirrorMethods.loss else 0,
        "value_loss_coef": 1.0,
        "ppo_epoch": 10,
        "clip_param": 0.2,
        "lr": lr,
        "eps": 1e-5,
        "max_grad_norm": 2.0,
    }


def post_config(args):
    args.env_name = get_env_name_for_method(args.env_name, args.mirror_method)


@ex.automain
def main(_seed, _config, _run):
    args = init(_seed, _config, _run, post_config=post_config)

    env_name = args.env_name

    dummy_env = make_env(env_name, render=False)

    cleanup_log_dir(args.log_dir)

    try:
        os.makedirs(args.save_dir)
    except OSError:
        pass

    torch.set_num_threads(1)

    envs = make_vec_envs(env_name, args.seed, args.num_processes, args.log_dir)

    obs_shape = envs.observation_space.shape
    obs_shape = (obs_shape[0], *obs_shape[1:])

    if args.load_saved_controller:
        best_model = "{}_best.pt".format(env_name)
        model_path = os.path.join(current_dir, "models", best_model)
        print("Loading model {}".format(best_model))
        actor_critic = torch.load(model_path)
    else:
        if args.mirror_method == MirrorMethods.net2:
            controller = SymmetricNetV2(
                *dummy_env.unwrapped.mirror_sizes,
                num_layers=6,
                hidden_size=256,
                tanh_finish=True
            )
        else:
            controller = SoftsignActor(dummy_env)
            if args.mirror_method == MirrorMethods.net:
                controller = SymmetricNet(controller, *dummy_env.unwrapped.sym_act_inds)
        actor_critic = Policy(controller)
        if args.sym_value_net:
            actor_critic.critic = SymmetricVNet(
                actor_critic.critic, controller.state_dim
            )

    mirror_function = None
    if (
        args.mirror_method == MirrorMethods.traj
        or args.mirror_method == MirrorMethods.loss
    ):
        indices = dummy_env.unwrapped.get_mirror_indices()
        mirror_function = get_mirror_function(indices)

    if args.cuda:
        actor_critic.cuda()

    agent = PPO(actor_critic, mirror_function=mirror_function, **args.ppo_params)

    rollouts = RolloutStorage(
        args.num_steps,
        args.num_processes,
        obs_shape,
        envs.action_space.shape[0],
        actor_critic.state_size,
    )
    current_obs = torch.zeros(args.num_processes, *obs_shape)

    def update_current_obs(obs):
        shape_dim0 = envs.observation_space.shape[0]
        obs = torch.from_numpy(obs).float()
        current_obs[:, -shape_dim0:] = obs

    obs = envs.reset()
    update_current_obs(obs)

    rollouts.observations[0].copy_(current_obs)

    if args.cuda:
        current_obs = current_obs.cuda()
        rollouts.cuda()

    episode_rewards = deque(maxlen=args.num_processes)
    num_updates = int(args.num_frames) // args.num_steps // args.num_processes

    start = time.time()
    next_checkpoint = args.save_every
    max_ep_reward = float("-inf")

    logger = ConsoleCSVLogger(
        log_dir=args.experiment_dir, console_log_interval=args.log_interval
    )

    for j in range(num_updates):

        if args.lr_decay_type == "linear":
            scheduled_lr = linear_decay(j, num_updates, args.lr, final_value=0)
        elif args.lr_decay_type == "exponential":
            scheduled_lr = exponential_decay(j, 0.99, args.lr, final_value=3e-5)
        else:
            scheduled_lr = args.lr

        set_optimizer_lr(agent.optimizer, scheduled_lr)

        for step in range(args.num_steps):
            # Sample actions
            with torch.no_grad():
                value, action, action_log_prob, states = actor_critic.act(
                    rollouts.observations[step],
                    rollouts.states[step],
                    rollouts.masks[step],
                )
            cpu_actions = action.squeeze(1).cpu().numpy()

            obs, reward, done, infos = envs.step(cpu_actions)
            reward = torch.from_numpy(np.expand_dims(np.stack(reward), 1)).float()

            bad_masks = np.ones((args.num_processes, 1))
            for p_index, info in enumerate(infos):
                keys = info.keys()
                # This information is added by algorithms.utils.TimeLimitMask
                if "bad_transition" in keys:
                    bad_masks[p_index] = 0.0
                # This information is added by baselines.bench.Monitor
                if "episode" in keys:
                    episode_rewards.append(info["episode"]["r"])

            masks = torch.FloatTensor([[0.0] if done_ else [1.0] for done_ in done])
            bad_masks = torch.from_numpy(bad_masks)

            update_current_obs(obs)
            rollouts.insert(
                current_obs,
                states,
                action,
                action_log_prob,
                value,
                reward,
                masks,
                bad_masks,
            )

        with torch.no_grad():
            next_value = actor_critic.get_value(
                rollouts.observations[-1], rollouts.states[-1], rollouts.masks[-1]
            ).detach()

        rollouts.compute_returns(next_value, args.use_gae, args.gamma, args.gae_lambda)

        value_loss, action_loss, dist_entropy = agent.update(rollouts)

        rollouts.after_update()

        frame_count = (j + 1) * args.num_steps * args.num_processes
        if (
            frame_count >= next_checkpoint or j == num_updates - 1
        ) and args.save_dir != "":
            model_name = "{}_{:d}.pt".format(env_name, int(next_checkpoint))
            next_checkpoint += args.save_every
        else:
            model_name = "{}_latest.pt".format(env_name)

        # A really ugly way to save a model to CPU
        save_model = actor_critic
        if args.cuda:
            save_model = copy.deepcopy(actor_critic).cpu()
        drive=1
        if drive:
          #print("save")
          torch.save(save_model, os.path.join("/content/gdrive/My Drive/darwin2", model_name))
        torch.save(save_model, os.path.join(args.save_dir, model_name))

        if len(episode_rewards) > 1 and np.mean(episode_rewards) > max_ep_reward:
            model_name = "{}_best.pt".format(env_name)
            max_ep_reward = np.mean(episode_rewards)
            drive=1
            if drive:
              #print("max_ep_reward",max_ep_reward)
              torch.save(save_model, os.path.join("/content/gdrive/My Drive/darwin2", model_name))
            torch.save(save_model, os.path.join(args.save_dir, model_name))  

        if len(episode_rewards) > 1:
            end = time.time()
            total_num_steps = (j + 1) * args.num_processes * args.num_steps
            logger.log_epoch(
                {
                    "iter": j + 1,
                    "total_num_steps": total_num_steps,
                    "fps": int(total_num_steps / (end - start)),
                    "entropy": dist_entropy,
                    "value_loss": value_loss,
                    "action_loss": action_loss,
                    "stats": {"rew": episode_rewards},
                }
            )
