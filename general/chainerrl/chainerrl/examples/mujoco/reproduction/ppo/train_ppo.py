"""A training script of PPO on OpenAI Gym Mujoco environments.

This script follows the settings of https://arxiv.org/abs/1709.06560 as much
as possible.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from builtins import *  # NOQA
from future import standard_library
standard_library.install_aliases()  # NOQA
import argparse
import functools

import chainer
from chainer import functions as F
from chainer import links as L
import gym
import gym.spaces
import numpy as np

import chainerrl
from chainerrl.agents import PPO
from chainerrl import experiments
from chainerrl import misc


def main():
    import logging

    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', type=int, default=0,
                        help='GPU to use, set to -1 if no GPU.')
    parser.add_argument('--env', type=str, default='Hopper-v2',
                        help='OpenAI Gym MuJoCo env to perform algorithm on.')
    parser.add_argument('--num-envs', type=int, default=1,
                        help='Number of envs run in parallel.')
    parser.add_argument('--seed', type=int, default=0,
                        help='Random seed [0, 2 ** 32)')
    parser.add_argument('--outdir', type=str, default='results',
                        help='Directory path to save output files.'
                             ' If it does not exist, it will be created.')
    parser.add_argument('--steps', type=int, default=2 * 10 ** 6,
                        help='Total number of timesteps to train the agent.')
    parser.add_argument('--eval-interval', type=int, default=100000,
                        help='Interval in timesteps between evaluations.')
    parser.add_argument('--eval-n-runs', type=int, default=100,
                        help='Number of episodes run for each evaluation.')
    parser.add_argument('--render', action='store_true',
                        help='Render env states in a GUI window.')
    parser.add_argument('--demo', action='store_true',
                        help='Just run evaluation, not training.')
    parser.add_argument('--load', type=str, default='',
                        help='Directory to load agent from.')
    parser.add_argument('--logger-level', type=int, default=logging.INFO,
                        help='Level of the root logger.')
    parser.add_argument('--monitor', action='store_true',
                        help='Wrap env with gym.wrappers.Monitor.')
    parser.add_argument('--log-interval', type=int, default=1000,
                        help='Interval in timesteps between outputting log'
                             ' messages during training')
    parser.add_argument('--update-interval', type=int, default=2048,
                        help='Interval in timesteps between model updates.')
    parser.add_argument('--epochs', type=int, default=10,
                        help='Number of epochs to update model for per PPO'
                             ' iteration.')
    parser.add_argument('--batch-size', type=int, default=64,
                        help='Minibatch size')
    args = parser.parse_args()

    logging.basicConfig(level=args.logger_level)

    # Set a random seed used in ChainerRL
    misc.set_random_seed(args.seed, gpus=(args.gpu,))

    # Set different random seeds for different subprocesses.
    # If seed=0 and processes=4, subprocess seeds are [0, 1, 2, 3].
    # If seed=1 and processes=4, subprocess seeds are [4, 5, 6, 7].
    process_seeds = np.arange(args.num_envs) + args.seed * args.num_envs
    assert process_seeds.max() < 2 ** 32

    args.outdir = experiments.prepare_output_dir(args, args.outdir)

    def make_env(process_idx, test):
        env = gym.make(args.env)
        # Use different random seeds for train and test envs
        process_seed = int(process_seeds[process_idx])
        env_seed = 2 ** 32 - 1 - process_seed if test else process_seed
        env.seed(env_seed)
        # Cast observations to float32 because our model uses float32
        env = chainerrl.wrappers.CastObservationToFloat32(env)
        if args.monitor:
            env = chainerrl.wrappers.Monitor(env, args.outdir)
        if args.render:
            env = chainerrl.wrappers.Render(env)
        return env

    def make_batch_env(test):
        return chainerrl.envs.MultiprocessVectorEnv(
            [functools.partial(make_env, idx, test)
             for idx, env in enumerate(range(args.num_envs))])

    # Only for getting timesteps, and obs-action spaces
    sample_env = gym.make(args.env)
    timestep_limit = sample_env.spec.tags.get(
        'wrapper_config.TimeLimit.max_episode_steps')
    obs_space = sample_env.observation_space
    action_space = sample_env.action_space
    print('Observation space:', obs_space)
    print('Action space:', action_space)

    assert isinstance(action_space, gym.spaces.Box)

    # Normalize observations based on their empirical mean and variance
    obs_normalizer = chainerrl.links.EmpiricalNormalization(
        obs_space.low.size, clip_threshold=5)

    # While the original paper initialized weights by normal distribution,
    # we use orthogonal initialization as the latest openai/baselines does.
    winit = chainerrl.initializers.Orthogonal(1.)
    winit_last = chainerrl.initializers.Orthogonal(1e-2)

    action_size = action_space.low.size
    policy = chainer.Sequential(
        L.Linear(None, 64, initialW=winit),
        F.tanh,
        L.Linear(None, 64, initialW=winit),
        F.tanh,
        L.Linear(None, action_size, initialW=winit_last),
        chainerrl.policies.GaussianHeadWithStateIndependentCovariance(
            action_size=action_size,
            var_type='diagonal',
            var_func=lambda x: F.exp(2 * x),  # Parameterize log std
            var_param_init=0,  # log std = 0 => std = 1
        ),
    )

    vf = chainer.Sequential(
        L.Linear(None, 64, initialW=winit),
        F.tanh,
        L.Linear(None, 64, initialW=winit),
        F.tanh,
        L.Linear(None, 1, initialW=winit),
    )

    # Combine a policy and a value function into a single model
    model = chainerrl.links.Branched(policy, vf)

    opt = chainer.optimizers.Adam(3e-4, eps=1e-5)
    opt.setup(model)

    agent = PPO(
        model,
        opt,
        obs_normalizer=obs_normalizer,
        gpu=args.gpu,
        update_interval=args.update_interval,
        minibatch_size=args.batch_size,
        epochs=args.epochs,
        clip_eps_vf=None,
        entropy_coef=0,
        standardize_advantages=True,
        gamma=0.995,
        lambd=0.97,
    )

    if args.load:
        agent.load(args.load)

    if args.demo:
        env = make_batch_env(True)
        eval_stats = experiments.eval_performance(
            env=env,
            agent=agent,
            n_steps=None,
            n_episodes=args.eval_n_runs,
            max_episode_len=timestep_limit)
        print('n_runs: {} mean: {} median: {} stdev {}'.format(
            args.eval_n_runs, eval_stats['mean'], eval_stats['median'],
            eval_stats['stdev']))
    else:
        experiments.train_agent_batch_with_evaluation(
            agent=agent,
            env=make_batch_env(False),
            eval_env=make_batch_env(True),
            outdir=args.outdir,
            steps=args.steps,
            eval_n_steps=None,
            eval_n_episodes=args.eval_n_runs,
            eval_interval=args.eval_interval,
            log_interval=args.log_interval,
            max_episode_len=timestep_limit,
            save_best_so_far_agent=False,
        )


if __name__ == '__main__':
    main()
