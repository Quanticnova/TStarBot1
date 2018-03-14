import os
import sys
import time
import random
import math
import numpy as np
from copy import deepcopy
import queue
import threading
import multiprocessing

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.optim as optim
from gym import spaces

from agents.memory import ReplayMemory, Transition


def tuple_cuda(tensors):
    if isinstance(tensors, tuple):
        return tuple(tensor.pin_memory().cuda(async=True) for tensor in tensors)
    else:
        return tensors.pin_memory().cuda(async=True)


def tuple_variable(tensors, volatile=False):
    if isinstance(tensors, tuple):
        return tuple(Variable(tensor, volatile=volatile)
                     for tensor in tensors)
    else:
        return Variable(tensors, volatile=volatile)


def actor_worker(pid, env_create_fn, q_network, current_eps, action_space,
                 allow_eval_mode, out_queue):

    def act(observation, eps):
        if random.uniform(0, 1) >= eps:
            if isinstance(observation, tuple):
                observation = tuple(torch.from_numpy(np.expand_dims(array, 0))
                                    for array in observation)
            else:
                observation = torch.from_numpy(np.expand_dims(observation, 0))
            if torch.cuda.is_available():
                observation = tuple_cuda(observation)
            if allow_eval_mode:
                q_network.eval()
            q = q_network(tuple_variable(observation, volatile=True))
            action = q.data.max(1)[1][0]
            return action
        else:
            return action_space.sample()

    env = env_create_fn()
    episode_id = 0
    while True:
        cum_return = 0.0
        observation = env.reset()
        done = False
        while not done:
            action = act(observation, eps=current_eps.value)
            next_observation, reward, done, _ = env.step(action)
            out_queue.put((observation, action, reward, next_observation, done))
            observation = next_observation
            cum_return += reward
        episode_id += 1
        print("Actor Worker ID %d Episode %d Epsilon %f Return: %f." %
              (pid, episode_id, current_eps.value, cum_return))
        sys.stdout.flush()


class FastDQNAgent(object):
    '''Deep Q-learning agent.'''

    def __init__(self,
                 observation_space,
                 action_space,
                 network,
                 learning_rate,
                 momentum,
                 batch_size,
                 discount,
                 eps_method,
                 eps_start,
                 eps_end,
                 eps_decay,
                 memory_size,
                 init_memory_size,
                 frame_step_ratio,
                 gradient_clipping,
                 double_dqn,
                 target_update_freq,
                 allow_eval_mode=True,
                 loss_type='mse',
                 init_model_path=None,
                 save_model_dir=None,
                 save_model_freq=50000,
                 print_freq=1000):
        assert isinstance(action_space, spaces.Discrete)
        multiprocessing.set_start_method('spawn')

        self._batch_size = batch_size
        self._discount = discount
        self._eps_method = eps_method
        self._eps_start = eps_start
        self._eps_end = eps_end
        self._eps_decay = eps_decay
        self._frame_step_ratio = frame_step_ratio
        self._target_update_freq = target_update_freq
        self._double_dqn = double_dqn
        self._save_model_dir = save_model_dir
        self._save_model_freq = save_model_freq
        self._action_space = action_space
        self._memory_size = memory_size
        self._init_memory_size = max(init_memory_size, batch_size)
        self._gradient_clipping = gradient_clipping
        self._allow_eval_mode = allow_eval_mode
        self._loss_type = loss_type
        self._print_freq = print_freq
        self._episode_idx = 0
        self._current_eps = multiprocessing.Value('d', 1.0)
        self._num_threads = 8

        self._q_network = network
        self._q_network.share_memory()
        if init_model_path:
            self._load_model(init_model_path)
            self._episode_idx = int(init_model_path[
                init_model_path.rfind('-')+1:])
        if torch.cuda.device_count() > 1:
            self._q_network = nn.DataParallel(self._q_network)
        if torch.cuda.is_available():
            self._q_network.cuda()

        if double_dqn:
            self._init_target_network(network)
            self._update_target_network()

        self._optimizer = optim.RMSprop(self._q_network.parameters(),
                                        momentum=momentum,
                                        lr=learning_rate)

    def act(self, observation, eps=0):
        if random.uniform(0, 1) >= eps:
            if isinstance(observation, tuple):
                observation = tuple(torch.from_numpy(np.expand_dims(array, 0))
                                    for array in observation)
            else:
                observation = torch.from_numpy(np.expand_dims(observation, 0))
            if torch.cuda.is_available():
                observation = tuple_cuda(observation)
            if self._allow_eval_mode:
                self._q_network.eval()
            q = self._q_network(tuple_variable(observation, volatile=True))
            action = q.data.max(1)[1][0]
            return action
        else:
            return self._action_space.sample()

    def learn(self, create_env_fn, num_actor_workers):
        self._init_parallel_actors(create_env_fn, num_actor_workers)
        steps, loss_sum = 0, 0.0
        t = time.time()
        while True:
            if self._double_dqn and steps % self._target_update_freq == 0:
                self._update_target_network()
            self._current_eps.value = self._get_current_eps(steps)
            loss_sum += self._optimize()
            steps += 1
            if self._save_model_dir and steps % self._save_model_freq == 0:
                self._save_model(os.path.join(
                    self._save_model_dir, 'agent.model-%d' % steps))
            if steps % self._print_freq == 0:
                print("Steps: %d Time: %f Epsilon: %f Loss %f "
                      % (steps, time.time() - t, self._current_eps.value,
                         loss_sum / self._print_freq))
                loss_sum = 0.0
                t = time.time()

    def _optimize(self):
        #print("Batch Queue Size: %d" % self._batch_queue.qsize())
        (next_obs_batch, obs_batch, reward_batch, action_batch, done_batch) = \
            self._batch_queue.get()
        # compute max-q target
        if self._allow_eval_mode:
            self._q_network.eval()
        q_next = self._q_network(next_obs_batch)
        if self._double_dqn:
            q_next_target = self._target_q_network(next_obs_batch)
            futures = q_next_target.gather(
                1, q_next.max(dim=1)[1].view(-1, 1)).squeeze()
        else:
            futures = q_next.max(dim=1)[0].view(-1, 1).squeeze()
        futures = futures * (1 - done_batch)
        target_q = reward_batch + self._discount * futures
        target_q.volatile = False
        # define loss
        self._q_network.train()
        q = self._q_network(obs_batch).gather(
            1, action_batch.view(-1, 1))
        if self._loss_type == "smooth_l1":
            loss = F.smooth_l1_loss(q, target_q)
        elif self._loss_type == "mse":
            loss = F.mse_loss(q, target_q)
        else:
            raise NotImplementedError
        # compute gradient and update parameters
        self._optimizer.zero_grad()
        loss.backward()
        for param in self._q_network.parameters():
            param.grad.data.clamp_(-self._gradient_clipping,
                                   self._gradient_clipping)
        self._optimizer.step()
        return loss.data[0]

    def _init_parallel_actors(self, create_env_fn, num_actor_workers):
        self._transition_queue = multiprocessing.Queue(
            1 if self._frame_step_ratio < 1
            else int(self._frame_step_ratio * self._num_threads))
        self._actor_processes = [
            multiprocessing.Process(
                target=actor_worker,
                args=(pid, create_env_fn, self._q_network, self._current_eps,
                      self._action_space, self._allow_eval_mode,
                      self._transition_queue))
            for pid in range(num_actor_workers)]
        self._batch_queue = queue.Queue(8)
        self._batch_thread = [threading.Thread(target=self._prepare_batch,
                                               args=(tid,))
                              for tid in range(self._num_threads)]
        for process in self._actor_processes:
            process.daemon = True
            process.start()
        for thread in self._batch_thread:
            thread.daemon = True
            thread.start()

    def _prepare_batch(self, tid):
        memory = ReplayMemory(int(self._memory_size / self._num_threads))
        steps = 0
        if self._frame_step_ratio < 1:
            steps_per_frame = int(1 / self._frame_step_ratio)
        else:
            frames_per_step = int(self._frame_step_ratio)
        while True:
            steps += 1
            if self._frame_step_ratio < 1:
                if steps % steps_per_frame == 0: 
                    memory.push(*(self._transition_queue.get()))
            else:
                #print("Trans Queue Size: %d" % self._transition_queue.qsize())
                for i in range(frames_per_step):
                    memory.push(*(self._transition_queue.get()))
            if len(memory) < self._init_memory_size / self._num_threads:
                continue
            transitions = memory.sample(self._batch_size)
            self._batch_queue.put(self._transitions_to_batch(transitions))

    def _transitions_to_batch(self, transitions):
        # batch to pytorch tensor
        batch = Transition(*zip(*transitions))
        if isinstance(batch.next_observation[0], tuple):
            next_obs_batch = tuple(torch.from_numpy(np.stack(feat))
                                   for feat in zip(*batch.next_observation))
        else:
            next_obs_batch = torch.from_numpy(np.stack(batch.next_observation))
        if isinstance(batch.observation[0], tuple):
            obs_batch = tuple(torch.from_numpy(np.stack(feat))
                              for feat in zip(*batch.observation))
        else:
            obs_batch = torch.from_numpy(np.stack(batch.observation))
        reward_batch = torch.FloatTensor(batch.reward)
        action_batch = torch.LongTensor(batch.action)
        done_batch = torch.Tensor(batch.done)

        # move to cuda
        if torch.cuda.is_available():
            next_obs_batch = tuple_cuda(next_obs_batch)
            obs_batch = tuple_cuda(obs_batch)
            reward_batch = tuple_cuda(reward_batch)
            action_batch = tuple_cuda(action_batch)
            done_batch = tuple_cuda(done_batch)

        # create variables
        next_obs_batch = tuple_variable(next_obs_batch, volatile=True)
        obs_batch = tuple_variable(obs_batch)
        reward_batch = tuple_variable(reward_batch)
        action_batch = tuple_variable(action_batch)
        done_batch = tuple_variable(done_batch)

        return (next_obs_batch, obs_batch, reward_batch, action_batch,
                done_batch)

    def _init_target_network(self, network):
        self._target_q_network = deepcopy(network)
        if torch.cuda.device_count() > 1:
            self._target_q_network = nn.DataParallel(self._target_q_network)
        if torch.cuda.is_available():
            self._target_q_network.cuda()
        if self._allow_eval_mode:
            self._target_q_network.eval()

    def _update_target_network(self):
        self._target_q_network.load_state_dict(
            self._q_network.state_dict())
                
    def _save_model(self, model_path):
        torch.save(self._q_network.state_dict(), model_path)

    def _load_model(self, model_path):
        self._q_network.load_state_dict(
            torch.load(model_path, map_location=lambda storage, loc: storage))

    def _get_current_eps(self, steps):
        if self._eps_method == 'exponential':
            eps = self._eps_end + (self._eps_start - self._eps_end) * \
                math.exp(-1. * steps / self._eps_decay)
        elif self._eps_method == 'linear':
            steps = min(self._eps_decay, steps)
            eps = self._eps_start - (self._eps_start - self._eps_end) * \
                steps / self._eps_decay
        else:
            raise NotImplementedError
        return eps
