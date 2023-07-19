import numpy as np
import gymnasium as gym
from PIL import Image
from core.agents.agent import *
import torch
import hydra

# DOWN = 0
# RIGHT = 1
# UP = 2
# LEFT = 3

@hydra.main(config_path="config", config_name="config.yaml")
def main(args):
    env = gym.make('core:MazEnv-v0')
    env.reset()
    agent = Agent(env=env, args=args)
    agent.train()


if __name__ == "__main__":
    main()
