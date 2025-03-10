from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
from builtins import *  # NOQA
from future import standard_library
standard_library.install_aliases()  # NOQA

import gym


class Render(gym.Wrapper):
    """Render env by calling its render method.

    Args:
        env (gym.Env): Env to wrap.
        **kwargs: Keyword arguments passed to the render method.
    """

    def __init__(self, env, **kwargs):
        super().__init__(env)
        self._kwargs = kwargs

    def reset(self, **kwargs):
        ret = self.env.reset(**kwargs)
        self.env.render(**self._kwargs)
        return ret

    def step(self, action):
        ret = self.env.step(action)
        self.env.render(**self._kwargs)
        return ret
