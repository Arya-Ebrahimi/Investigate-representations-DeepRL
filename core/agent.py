import torch
import torch.nn as nn
import os
from pathlib import Path
import random
from tqdm import trange
from itertools import count
import matplotlib.pyplot as plt
import matplotlib
import math
import datetime
from core.utils import *
from core.nn import Network
import pickle

is_ipython = 'inline' in matplotlib.get_backend()
if is_ipython:
    from IPython import display

plt.ion()

class Agent():
    
    '''
    This class contains the information of an agent and its training process
    
    An instance of this class recieves a MazEnv environment and arguments (config file) as an input
    and has following methods:
    methods:
        -select_action -> handles epsilon-greedy action selection
        -plot_rewards -> plots each episode's reward online to see the trends of learning
        -optimize -> handles the optimization part of neural networks (feed forward, calculate loss, backpropagation)
        -train -> main loop of training
        -_save -> a supplemantary function to save the networks and reward in predefined directories
    
    '''
    
    def __init__(self, env, args):
        self.args = args  # args contains config.yaml in config directory
        self.env = env
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')  # select gpu if available
        self.id = datetime.datetime.now().strftime("%Y-%m-%d_%H:%M")  # id for the name of agent that is going to be saved
        self.model_dir = Path('.models')  # directory to save target networks
        self.reward_dir = Path('.rewards')  # directory to save rewards
        self.rewards=[]
        
        
        # some auxiliary tasks require the next actions as well which is considered here
        if self.args.use_aux == 'sf' or self.args.use_aux == 'sf+reward' or self.args.use_aux=='virtual-reward-1' or self.args.use_aux=='virtual-reward-5':
            self.need_next = True
        else:
            self.need_next = False
        
        # check if save directories are not available to create them
        if not os.path.exists(self.model_dir):
            os.makedirs(self.model_dir)
        if not os.path.exists(self.reward_dir):
            os.makedirs(self.reward_dir)
            
        self.action_space = env.action_space.n
        
        # create policy and target networks
        self.policy_net = Network(self.args.use_fta, self.args.use_aux).to(self.device)
        self.target_net = Network(self.args.use_fta, self.args.use_aux).to(self.device)
        self.target_net.load_state_dict(self.policy_net.state_dict())
        
        # define loss function and optimizer with hyperparameters set in config.yaml file
        self.loss_fn = nn.SmoothL1Loss()
        self.optimizer = torch.optim.AdamW(self.policy_net.parameters(), lr=self.args.learning_rate, amsgrad=True)
        
        # create the replay buffer
        self.memory = ReplayMemory(1000000)
        
        self.steps_done = 0
        self.reward_in_episode = []
        
    def select_action(self, state):
        
        '''
        This function handles actions selection and also increases the steps_done which
        decreases the epsilon for future action selections.
        
        inputs:
            -state: current state of our agent
        outputs:
            -an action based on epsilon-greedy action selection
        '''
        
        sample = random.random()
        eps_threshold = self.args.eps_end + (self.args.eps_start - self.args.eps_end) * \
            math.exp(-1. * self.steps_done / self.args.eps_decay)
        self.steps_done += 1
        if sample > eps_threshold:
            with torch.no_grad():
                '''
                t.max(1) will return the largest column value of each row.
                second column on max result is index of where max element was
                found, so we pick action with the larger expected reward.
                '''
                return self.policy_net(state)[0].max(1)[1].view(1, 1)
        else:
            return torch.tensor([[self.env.action_space.sample()]], device=self.device, dtype=torch.long)
            
            
    def plot_rewards(self, show_result=False):
        
        '''
        This function handles the online reward plottings
        To plot rewards, set plot_during_training to True in config.yaml file 
        '''
        
        plt.figure(1)
        rewards_t = torch.tensor(self.reward_in_episode, dtype=torch.float)
        if show_result:
            plt.title('Result')
        else:
            plt.clf()
            plt.title('Training...')
        plt.xlabel('Episode')
        plt.ylabel('Rewards')
        plt.plot(rewards_t.numpy())
        if len(rewards_t) >= 100:
            means = rewards_t.unfold(0, 100, 1).mean(1).view(-1)
            means = torch.cat((torch.zeros(99), means))
            plt.plot(means.numpy())

        plt.pause(0.001)
        if is_ipython:
            if not show_result:
                display.display(plt.gcf())
                display.clear_output(wait=True)
            else:
                display.display(plt.gcf())
                
    def optimize(self, i):
        
        '''
        OPTIMIZATION
        '''
        
        # check if replay buffer has enough transition samples
        if len(self.memory) < self.args.batch_size:
            return
        transitions = self.memory.sample(self.args.batch_size)
        batch = Transition(*zip(*transitions))

        # Compute a mask of non-final states and concatenate the batch elements
        # (a final state would've been the one after which simulation ended)
        
        non_final_mask = torch.tensor(tuple(map(lambda s: s is not None,
                                            batch.next_state)), device=self.device, dtype=torch.bool)
        non_final_next_states = torch.cat([s for s in batch.next_state
                                                    if s is not None])
        state_batch = torch.cat(batch.state)
        action_batch = torch.cat(batch.action)
        reward_batch = torch.cat(batch.reward)
        
        
        if self.need_next:
            next_action_batch = torch.cat(batch.next_action)
            next_state_batch = torch.cat(batch.next_state)
            

        # the net_return is in the form of -> [x, aux, rep, reward]
        net_return = self.policy_net(state_batch)
        state_action_values = net_return[0].gather(1, action_batch)

        next_state_values = torch.zeros(self.args.batch_size, device=self.device)
        with torch.no_grad():
            next_state_values[non_final_mask] = self.target_net(non_final_next_states)[0].max(1)[0]
            
        # Compute the bootstraped Q values
        expected_state_action_values = (next_state_values * self.args.gamma) + reward_batch
 
        criterion = nn.MSELoss()
        
        # calcualte the loss -> make state_action_values more like bootstraped values
        loss = criterion(state_action_values, expected_state_action_values.unsqueeze(1))

        # if it is not a transfer task, then we might have some auxiliary tasks as well
        if not self.args.transfer:
            
            # check if we have auxiliary tasks and calculate their losses
            # we add the auxiliary losses to the main loss (there might be a special weight for each which is set in config.yaml)
            if self.args.use_aux != None:
                
                # Input Reconstruction auxiliary task
                if self.args.use_aux == 'ir':
                    aux_return = net_return[1]
                    aux_loss = nn.MSELoss()
                    
                    loss = loss + self.args.aux_loss_weight * aux_loss(aux_return, state_batch)
                
                # Reward Prediction auxiliary task
                if self.args.use_aux == 'reward':
                    aux_return = net_return[1]
                    aux_loss = nn.MSELoss()
                    rb = torch.reshape(reward_batch, (self.args.batch_size, -1))
                    loss = loss + aux_loss(aux_return, rb)
                    
                # Successor Features auxiliary task
                if self.args.use_aux == 'sf':
                    aux_return = net_return[1]
                    representation_st = net_return[2]
                    next_state_rec = net_return[3]

                    with torch.no_grad(): 
                        next_state_aux_return = self.target_net(next_state_batch)[1].gather(1, next_action_batch)
                    
                    aux_loss = nn.MSELoss()
                    next_rep_loss = nn.MSELoss()

                    loss_to_add = self.args.aux_loss_weight * aux_loss(aux_return, representation_st + self.args.gamma * next_state_aux_return) 

                    loss = loss + loss_to_add + next_rep_loss(next_state_rec, next_state_batch)
                            
                # Virtual Value Functions auxiliary tasks
                if self.args.use_aux == 'virtual-reward-1' or self.args.use_aux == 'virtual-reward-5':
                    virtual_reward_batch = torch.cat(batch.virtual_reward)
                    action_values = net_return[1].gather(1, action_batch)
                    with torch.no_grad():
                        next_state_virtual_action_values = self.target_net(next_state_batch)[1].gather(1, next_action_batch)
                        bootstraped_value = (virtual_reward_batch + self.args.gamma * next_state_virtual_action_values.squeeze())

                    aux_loss = nn.MSELoss()       
                    
                    loss = loss + aux_loss(action_values, bootstraped_value.unsqueeze(1))
        
        # Optimize the model
        self.optimizer.zero_grad()
        loss.backward()
        # In-place gradient clipping
        torch.nn.utils.clip_grad_value_(self.policy_net.parameters(), 100)
        self.optimizer.step()
        
    
    def _save(self):
        '''
        save model and rewards in predefined directories
        '''
        if self.args.save_rewards:
            with open(f'{self.reward_dir}/rewards_{self.id}.pkl', 'wb') as fp:
                pickle.dump(self.reward_in_episode, fp)
                        
                
        if self.args.save_model:
            torch.save(self.target_net.state_dict(), f'{self.model_dir}/pytorch_{self.id}.pt')
    
    def train(self):
        
        '''
        The main training loop of our agent
        
        iterates for max_episodes or reaching a number of consecutive sucseeful episodes with a reward of 1
        this can be set in config.yaml file
        
        self.optimize if called in each iteration to optimize the agent's networks
        '''
        
        consecutive_episodes = 0
        
        # iterate for args.max_episodes episode
        # this loop will be terminated if agent reach a args.consecutive_episodes number of successful consecutive episodes
        for i in trange(self.args.max_episodes):
            
            reward_in_episode = 0   # reward recieved in current episode
            state, info = self.env.reset()  # initiate the first position of agent
            state = state.transpose((2, 0, 1))  # torch uses BGR but our state is RGB
            state = torch.tensor(state, dtype=torch.float32, device=self.device).unsqueeze(0)   # convert numpy state to torch tensor
            
            # iterate over a number of steps untill reaching terminal state or horizon defined in config.yaml
            for t in count():
                # select action based on an epsilon-greedy approach
                action = self.select_action(state)
                if self.need_next:  # check if the current auxiliary task requires next state and next action
                    if t > 0:
                        # vvf auxiliary tasks also require virtual reward which is handled here and added to replay buffer
                        if self.args.use_aux == 'virtual-reward-1' or self.args.use_aux == 'virtual-reward-5':
                            virtual_reward = torch.tensor([info['virtual-reward']], device=self.device)

                            self.memory.push(previous_state, previous_action, state, reward, action, virtual_reward)
                            self.optimize(i)
                        
                        else:
                            self.memory.push(previous_state, previous_action, state, reward, action, None)
                            self.optimize(i)

                # step in the environment to recieve reward and next_state (observation)
                observation, reward, terminated, truncated, info = self.env.step(action.item())
                observation = observation.transpose((2, 0, 1))

                # convert numpy arrays to tensors
                reward = torch.tensor([reward], device=self.device)
                done = terminated or truncated
                next_state = torch.tensor(observation, dtype=torch.float32, device=self.device).unsqueeze(0)

                # store transitions in memory
                if not self.need_next:
                    self.memory.push(state, action, next_state, reward, None, None)
                    self.optimize(i)
                
                # update previous actions and next state
                previous_action = action
                previous_state = state
                state = next_state
                reward_in_episode += reward
                
                # update target network weights
                if self.args.soft_target_update:
                    target_net_state_dict = self.target_net.state_dict()
                    policy_net_state_dict = self.policy_net.state_dict()
                    for key in policy_net_state_dict:
                        target_net_state_dict[key] = policy_net_state_dict[key]*self.args.tau + target_net_state_dict[key]*(1-self.args.tau)
                    self.target_net.load_state_dict(target_net_state_dict)
                
                # check if reached horizon to truncate
                if done or t > self.args.horizon:
                    if self.need_next:
                        if self.args.use_aux == 'virtual-reward-1' or self.args.use_aux == 'virtual-reward-5':
                            virtual_reward = torch.tensor([info['virtual-reward']], device=self.device)
                            self.memory.push(previous_state, previous_action, state, reward, action, virtual_reward)
                            self.optimize(i)
                        else:
                            self.memory.push(previous_state, previous_action, state, reward, action, None)
                            self.optimize(i)
                    
                    self.reward_in_episode.append(reward_in_episode)
                    if(reward_in_episode == 1):
                        consecutive_episodes += 1
                    else:
                        consecutive_episodes = 0
                    
                    if self.args.plot_during_training:
                        self.plot_rewards()
                    break
            
            # check for consecutive successful episodes
            if consecutive_episodes == self.args.consecutive_episodes:
                self._save()
                break
            
            if not self.args.soft_target_update:
                if i % self.args.target_update == 0:
                    self.target_net.load_state_dict(self.policy_net.state_dict())
            
            # save networks with a frequency defined in config.yaml
            if i % self.args.save_ratio == 0:
                self._save()
        
        if self.args.plot_during_training:
            self.plot_rewards(show_result=True)
            plt.ioff()
            plt.show()
        