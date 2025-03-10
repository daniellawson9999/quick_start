"""original source: https://github.com/chainer/chainerrl/blob/master/examples/atari/reproduction/rainbow/train_rainbow.py

MIT License

Copyright (c) Preferred Networks, Inc.
"""

import argparse
from logging import getLogger
import os

import minerl  # noqa: register MineRL envs as Gym envs.
import gym
import numpy as np
import chainer

import chainerrl
from chainerrl.wrappers import ContinuingTimeLimit, RandomizeAction
from chainerrl.wrappers.atari_wrappers import FrameStack, ScaledFloatFrame

import sys
sys.path.append(os.path.abspath(os.path.join(__file__, os.pardir)))
import utils
from q_functions import DuelingDQN, DistributionalDuelingDQN
from env_wrappers import (
    SerialDiscreteActionWrapper, CombineActionWrapper, SerialDiscreteCombineActionWrapper,
    ContinuingTimeLimitMonitor,
    MoveAxisWrapper, FrameSkip, ObtainPoVWrapper, PoVWithCompassAngleWrapper, GrayScaleWrapper)

logger = getLogger(__name__)


def parse_arch(arch, n_actions, n_input_channels):
    if arch == 'dueling':
        # Conv2Ds of (channel, kernel, stride): [(32, 8, 4), (64, 4, 2), (64, 3, 1)]
        return DuelingDQN(n_actions, n_input_channels=n_input_channels, hiddens=[256])
    elif arch == 'distributed_dueling':
        n_atoms = 51
        v_min = -10
        v_max = 10
        return DistributionalDuelingDQN(n_actions, n_atoms, v_min, v_max, n_input_channels=n_input_channels)
    else:
        raise RuntimeError('Unsupported architecture name: {}'.format(arch))


def parse_agent(agent):
    return {'DQN': chainerrl.agents.DQN,
            'DoubleDQN': chainerrl.agents.DoubleDQN,
            'PAL': chainerrl.agents.PAL,
            'CategoricalDoubleDQN': chainerrl.agents.CategoricalDoubleDQN}[agent]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=str, default='MineRLTreechop-v0',
                        choices=[
                            'MineRLTreechop-v0',
                            'MineRLNavigate-v0', 'MineRLNavigateDense-v0',
                            'MineRLNavigateExtreme-v0', 'MineRLNavigateExtremeDense-v0',
                            'MineRLObtainIronPickaxe-v0', 'MineRLObtainIronPickaxeDense-v0',
                            'MineRLObtainDiamond-v0', 'MineRLObtainDiamondDense-v0',
                            # for debug use
                            'MineRLNavigateDenseFixed-v0',
                            'MineRLObtainTest-v0',
                        ],
                        help='MineRL environment identifier.')
    parser.add_argument('--outdir', type=str, default='results',
                        help='Directory path to save output files. If it does not exist, it will be created.')
    parser.add_argument('--seed', type=int, default=0, help='Random seed [0, 2 ** 31)')
    parser.add_argument('--gpu', type=int, default=0, help='GPU to use, set to -1 if no GPU.')
    parser.add_argument('--demo', action='store_true', default=False)
    parser.add_argument('--load', type=str, default=None)
    parser.add_argument('--final-exploration-frames', type=int, default=10 ** 6,
                        help='Timesteps after which we stop annealing exploration rate')
    parser.add_argument('--final-epsilon', type=float, default=0.01, help='Final value of epsilon during training.')
    parser.add_argument('--eval-epsilon', type=float, default=0.001, help='Exploration epsilon used during eval episodes.')
    parser.add_argument('--noisy-net-sigma', type=float, default=None,
                        help='NoisyNet explorer switch. This disables following options: '
                        '--final-exploration-frames, --final-epsilon, --eval-epsilon')
    parser.add_argument('--arch', type=str, default='dueling', choices=['dueling', 'distributed_dueling'],
                        help='Network architecture to use.')
    parser.add_argument('--replay-capacity', type=int, default=10 ** 6, help='Maximum capacity for replay buffer.')
    parser.add_argument('--replay-start-size', type=int, default=5 * 10 ** 4,
                        help='Minimum replay buffer size before performing gradient updates.')
    parser.add_argument('--target-update-interval', type=int, default=3 * 10 ** 4,
                        help='Frequency (in timesteps) at which the target network is updated.')
    parser.add_argument('--update-interval', type=int, default=4, help='Frequency (in timesteps) of network updates.')
    parser.add_argument('--eval-n-runs', type=int, default=3)
    parser.add_argument('--no-clip-delta', dest='clip_delta', action='store_false')
    parser.set_defaults(clip_delta=True)
    parser.add_argument('--num-step-return', type=int, default=1)
    parser.add_argument('--agent', type=str, default='DQN', choices=['DQN', 'DoubleDQN', 'PAL', 'CategoricalDoubleDQN'])
    parser.add_argument('--logging-level', type=int, default=20, help='Logging level. 10:DEBUG, 20:INFO etc.')
    parser.add_argument('--gray-scale', action='store_true', default=False, help='Convert pov into gray scaled image.')
    parser.add_argument('--monitor', action='store_true', default=False,
                        help='Monitor env. Videos and additional information are saved as output files when evaluation.')
    parser.add_argument('--lr', type=float, default=2.5e-4, help='Learning rate.')
    parser.add_argument('--adam-eps', type=float, default=1e-8, help='Epsilon for Adam.')
    parser.add_argument('--prioritized', action='store_true', default=False, help='Use prioritized experience replay.')
    parser.add_argument('--frame-stack', type=int, default=None, help='Number of frames stacked (None for disable).')
    parser.add_argument('--frame-skip', type=int, default=None, help='Number of frames skipped (None for disable).')
    parser.add_argument('--gamma', type=float, default=0.99, help='Discount rate.')
    parser.add_argument('--batch-accumulator', type=str, default='sum', choices=['sum', 'mean'], help='accumulator for batch loss.')
    parser.add_argument('--disable-action-prior', action='store_true', default=False,
                        help='If specified, action_space shaping based on prior knowledge will be disabled.')
    parser.add_argument('--always-keys', type=str, default=None, nargs='*',
                        help='List of action keys, which should be always pressed throughout interaction with environment.')
    parser.add_argument('--reverse-keys', type=str, default=None, nargs='*',
                        help='List of action keys, which should be always pressed but can be turn off via action.')
    parser.add_argument('--exclude-keys', type=str, default=None, nargs='*',
                        help='List of action keys, which should be ignored for discretizing action space.')
    parser.add_argument('--exclude-noop', action='store_true', default=False, help='The "noop" will be excluded from discrete action list.')
    args = parser.parse_args()

    args.outdir = chainerrl.experiments.prepare_output_dir(args, args.outdir)

    import logging
    log_format = '%(levelname)-8s - %(asctime)s - [%(name)s %(funcName)s %(lineno)d] %(message)s'
    logging.basicConfig(filename=os.path.join(args.outdir, 'log.txt'), format=log_format, level=args.logging_level)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(args.logging_level)
    console_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger('').addHandler(console_handler)  # add hander to the root logger

    logger.info('Output files are saved in {}'.format(args.outdir))

    utils.log_versions()

    try:
        _main(args)
    except:  # noqa
        logger.exception('execution failed.')
        raise


def _main(args):
    logger.info('The first `gym.make(MineRL*)` may take several minutes. Be patient!')

    os.environ['MALMO_MINECRAFT_OUTPUT_LOGDIR'] = args.outdir

    # Set a random seed used in ChainerRL.
    chainerrl.misc.set_random_seed(args.seed)

    # Set different random seeds for train and test envs.
    train_seed = args.seed  # noqa: never used in this script
    test_seed = 2 ** 31 - 1 - args.seed

    def wrap_env(env, test):
        # wrap env: time limit...
        if isinstance(env, gym.wrappers.TimeLimit):
            logger.info('Detected `gym.wrappers.TimeLimit`! Unwrap it and re-wrap our own time limit.')
            env = env.env
            max_episode_steps = env.spec.max_episode_steps
            env = ContinuingTimeLimit(env, max_episode_steps=max_episode_steps)

        # wrap env: observation...
        # NOTE: wrapping order matters!

        if test and args.monitor:
            env = ContinuingTimeLimitMonitor(
                env, os.path.join(args.outdir, env.spec.id, 'monitor'),
                mode='evaluation' if test else 'training', video_callable=lambda episode_id: True)
        if args.frame_skip is not None:
            env = FrameSkip(env, skip=args.frame_skip)
        if args.gray_scale:
            env = GrayScaleWrapper(env, dict_space_key='pov')
        if args.env.startswith('MineRLNavigate'):
            env = PoVWithCompassAngleWrapper(env)
        else:
            env = ObtainPoVWrapper(env)
        env = MoveAxisWrapper(env, source=-1, destination=0)  # convert hwc -> chw as Chainer requires.
        env = ScaledFloatFrame(env)
        if args.frame_stack is not None and args.frame_stack > 0:
            env = FrameStack(env, args.frame_stack, channel_order='chw')

        # wrap env: action...
        if not args.disable_action_prior:
            env = SerialDiscreteActionWrapper(
                env,
                always_keys=args.always_keys, reverse_keys=args.reverse_keys, exclude_keys=args.exclude_keys, exclude_noop=args.exclude_noop)
        else:
            env = CombineActionWrapper(env)
            env = SerialDiscreteCombineActionWrapper(env)

        if test and args.noisy_net_sigma is None:
            env = RandomizeAction(env, args.eval_epsilon)

        env_seed = test_seed if test else train_seed
        # env.seed(int(env_seed))  # TODO: not supported yet
        return env

    core_env = gym.make(args.env)
    env = wrap_env(core_env, test=False)
    # eval_env = gym.make(args.env)  # Can't create multiple MineRL envs
    # eval_env = wrap_env(eval_env, test=True)
    eval_env = wrap_env(core_env, test=True)

    # Q function
    n_actions = env.action_space.n
    q_func = parse_arch(args.arch, n_actions, n_input_channels=env.observation_space.shape[0])

    # explorer
    if args.noisy_net_sigma is not None:
        chainerrl.links.to_factorized_noisy(q_func, sigma_scale=args.noisy_net_sigma)
        # Turn off explorer
        explorer = chainerrl.explorers.Greedy()
    else:
        explorer = chainerrl.explorers.LinearDecayEpsilonGreedy(
            1.0, args.final_epsilon, args.final_exploration_frames, env.action_space.sample)

    # Draw the computational graph and save it in the output directory.
    sample_obs = env.observation_space.sample()
    sample_batch_obs = np.expand_dims(sample_obs, 0)
    chainerrl.misc.draw_computational_graph([q_func(sample_batch_obs)], os.path.join(args.outdir, 'model'))

    # Use the Nature paper's hyperparameters
    # opt = optimizers.RMSpropGraves(lr=args.lr, alpha=0.95, momentum=0.0, eps=1e-2)
    opt = chainer.optimizers.Adam(alpha=args.lr, eps=args.adam_eps)  # NOTE: mirrors DQN implementation in MineRL paper

    opt.setup(q_func)

    # calculate corresponding `steps` and `eval_interval` according to frameskip
    # = 1440 episodes if we count an episode as 6000 frames,
    # = 1080 episodes if we count an episode as 8000 frames.
    maximum_frames = 8640000
    if args.frame_skip is None:
        steps = maximum_frames
        eval_interval = 6000 * 100  # (approx.) every 100 episode (counts "1 episode = 6000 steps")
    else:
        steps = maximum_frames // args.frame_skip
        eval_interval = 6000 * 100 // args.frame_skip  # (approx.) every 100 episode (counts "1 episode = 6000 steps")

    # Select a replay buffer to use
    if args.prioritized:
        # Anneal beta from beta0 to 1 throughout training
        betasteps = steps / args.update_interval
        rbuf = chainerrl.replay_buffer.PrioritizedReplayBuffer(
            args.replay_capacity, alpha=0.5, beta0=0.4, betasteps=betasteps, num_steps=args.num_step_return)
    else:
        rbuf = chainerrl.replay_buffer.ReplayBuffer(args.replay_capacity, args.num_step_return)

    # build agent
    def phi(x):
        # observation -> NN input
        return np.asarray(x)
    Agent = parse_agent(args.agent)
    agent = Agent(
        q_func, opt, rbuf, gpu=args.gpu, gamma=args.gamma, explorer=explorer, replay_start_size=args.replay_start_size,
        target_update_interval=args.target_update_interval, clip_delta=args.clip_delta, update_interval=args.update_interval,
        batch_accumulator=args.batch_accumulator, phi=phi)
    if args.load:
        agent.load(args.load)

    # experiment
    if args.demo:
        eval_stats = chainerrl.experiments.eval_performance(env=eval_env, agent=agent, n_steps=None, n_episodes=args.eval_n_runs)
        logger.info('n_runs: {} mean: {} median: {} stdev {}'.format(
            args.eval_n_runs, eval_stats['mean'], eval_stats['median'], eval_stats['stdev']))
    else:
        chainerrl.experiments.train_agent_with_evaluation(
            agent=agent, env=env, steps=steps,
            eval_n_steps=None, eval_n_episodes=args.eval_n_runs, eval_interval=eval_interval,
            outdir=args.outdir, eval_env=eval_env, save_best_so_far_agent=True,
        )

    env.close()
    eval_env.close()


if __name__ == '__main__':
    main()
