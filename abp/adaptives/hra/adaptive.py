import logging

logger = logging.getLogger('root')

# import tensorflow as tf
import numpy as np
import torch
from torch.autograd import Variable

from abp.adaptives.common.memory import Memory
from abp.adaptives.common.experience import Experience
from abp.utils import clear_summary_path
from abp.models import HRAModel
from tensorboardX import SummaryWriter
import excitationbp as eb

# TODO Too many duplicate code. Need to refactor!

class HRAAdaptive(object):
    """HRAAdaptive using HRA architecture"""

    def __init__(self, name, choices, network_config, reinforce_config, log=True):
        super(HRAAdaptive, self).__init__()
        self.name = name
        self.choices = choices
        self.network_config = network_config
        self.reinforce_config = reinforce_config
        self.update_frequency = reinforce_config.update_frequency

        self.replay_memory = Memory(self.reinforce_config.memory_size)
        self.learning = True
        self.explanation = False

        self.steps = 0
        self.previous_state = None
        self.previous_action = None
        self.reward_types = len(self.network_config.networks)
        self.current_reward = [0] * self.reward_types  # TODO: change reward into dictionary
        self.total_reward = 0

        self.eval_model = HRAModel(self.name + "_eval", self.network_config)
        self.target_model = HRAModel(self.name + "_target", self.network_config)
        self.log = log
        if self.log:
            self.summary = SummaryWriter()
        self.episode = 0

    def __del__(self):
        self.summary.close()

    def should_explore(self):
        epsilon = np.max([0.1, self.reinforce_config.starting_epsilon * (
                    self.reinforce_config.decay_rate ** (self.steps / self.reinforce_config.decay_steps))])
        if self.log:
            self.summary.add_scalar(tag='epsilon', scalar_value=epsilon, global_step=self.steps)
        return np.random.choice([True, False], p=[epsilon, 1 - epsilon])

    def predict(self, state):
        self.steps += 1
        saliencies = []

        # add to experience
        if self.previous_state is not None and self.previous_action is not None:
            experience = Experience(self.previous_state, self.previous_action, self.current_reward, state)
            self.replay_memory.add(experience)

        if self.learning and self.should_explore():
            action = np.random.choice(len(self.choices))
            q_values = [None] * len(self.choices)  # TODO should it be output shape or from choices?
            choice = self.choices[action]
        else:
            _state = Variable(torch.Tensor(state)).unsqueeze(0)
            action, q_values = self.eval_model.predict(_state)

            choice = self.choices[action]

        if self.learning and self.steps % self.update_frequency == 0:
            logger.debug("Replacing target model for %s" % self.name)
            self.target_model.replace(self.eval_model)

        if self.explanation:
            eb.use_eb(True)
            prob_outputs = Variable(torch.zeros((4,)))
            for choice in range(len(self.choices)):
                action_saliencies = []
                prob_outputs[action] = 1
                for reward_type in range(self.reward_types):
                    self.eval_model.clear_weights(reward_type)
                    saliency = eb.excitation_backprop(self.eval_model.model, _state, prob_outputs, contrastive=True)
                    self.eval_model.restore_weights()

                    saliency = np.squeeze(saliency.view(*_state.shape).data.numpy())
                    action_saliencies.append(saliency)

                # for overall reward
                saliency = eb.excitation_backprop(self.eval_model.model, _state, prob_outputs, contrastive=True)
                saliency = np.squeeze(saliency.view(*_state.shape).data.numpy())
                action_saliencies.append(saliency)

                saliencies.append(action_saliencies)

        self.update()

        self.current_reward = [0] * self.reward_types

        self.previous_state = state
        self.previous_action = action

        return choice, q_values, saliencies

    def disable_learning(self):
        logger.info("Disabled Learning for %s agent" % self.name)
        self.eval_model.save_network()
        self.target_model.save_network()

        self.learning = False
        self.episode = 0

    def end_episode(self, state):
        if not self.learning:
            return

        logger.info("End of Episode %d with total reward %d" % (self.episode + 1, self.total_reward))

        self.episode += 1
        print('episode:', self.episode)
        if self.log:
            self.summary.add_scalar(tag='%s agent reward' % self.name,scalar_value=self.total_reward,
                                    global_step=self.episode)
            print('agent reward:', self.total_reward)

        experience = Experience(self.previous_state, self.previous_action, self.current_reward, state, is_terminal=True)
        self.replay_memory.add(experience)

        self.current_reward = [0] * self.reward_types
        self.total_reward = 0

        self.previous_state = None
        self.previous_action = None

        self.update()

    def reward(self, decomposed_rewards):
        self.total_reward += sum(decomposed_rewards)
        for i in range(self.reward_types):
            self.current_reward[i] += decomposed_rewards[i]

    def update(self):
        if self.replay_memory.current_size < self.reinforce_config.batch_size:
            return

        batch = self.replay_memory.sample(self.reinforce_config.batch_size)

        # TODO: Convert to tensor operations instead of for loops

        states = [experience.state for experience in batch]

        next_states = [experience.next_state for experience in batch]
        states = Variable(torch.Tensor(states))
        next_states = Variable(torch.Tensor(next_states))

        is_terminal = [0 if experience.is_terminal else 1 for experience in batch]

        actions = [experience.action for experience in batch]

        reward = np.array([experience.reward for experience in batch])

        q_next = self.target_model.predict_batch(next_states)

        q_2 = np.mean(q_next, axis = 2)

        q_2 = is_terminal * q_2

        q_values = self.eval_model.predict_batch(states)

        q_target = q_values.copy()

        batch_index = np.arange(self.reinforce_config.batch_size, dtype=np.int32)

        q_target[:, batch_index, actions] = np.transpose(reward) + self.reinforce_config.discount_factor * q_2

        self.eval_model.fit(states, q_target, self.steps)
