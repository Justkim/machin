import time
import torch as t
import torch.nn as nn

from models.frameworks.ddpg_td3 import DDPG_TD3
from models.noise import OrnsteinUhlenbeckNoise

from utils.logging import default_logger as logger
from utils.image import create_gif
from utils.tensor_board import global_board
from utils.helper_classes import Counter
from utils.prep import prep_dir_default
from utils.args import get_args

from env.walker.single_walker import BipedalWalker

# configs
restart = True
# max_batch = 8
max_epochs = 20
max_episodes = 1000
max_steps = 2000
replay_size = 500000
agent_num = 1
explore_noise_params = [(0, 0.2)] * 4
policy_noise_params = [(0, 0.1)] * 4
device = t.device("cuda:0")
root_dir = "/data/AI/tmp/multi_agent/walker/naive3/"
model_dir = root_dir + "model/"
log_dir = root_dir + "log/"
save_map = {}

observe_dim = 24
action_dim = 4
# train configs
# lr: learning rate, int: interval
# warm up should be less than one epoch
ddpg_update_batch_size = 100
ddpg_warmup_steps = 20
model_save_int = 500  # in episodes
profile_int = 50  # in episodes


class Actor(nn.Module):
    def __init__(self, state_dim, action_dim, max_action):
        super(Actor, self).__init__()

        self.l1 = nn.Linear(state_dim, 400)
        self.l2 = nn.Linear(400, 300)
        self.l3 = nn.Linear(300, action_dim)

        self.max_action = max_action

    def forward(self, state):
        a = t.relu(self.l1(state))
        a = t.relu(self.l2(a))
        a = t.tanh(self.l3(a)) * self.max_action
        return a


class Critic(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Critic, self).__init__()

        self.l1 = nn.Linear(state_dim + action_dim, 400)
        self.l2 = nn.Linear(400, 300)
        self.l3 = nn.Linear(300, 1)

    def forward(self, state, action):
        state_action = t.cat([state, action], 1)

        q = t.relu(self.l1(state_action))
        q = t.relu(self.l2(q))
        q = self.l3(q)
        return q


if __name__ == "__main__":
    args = get_args()
    for k, v in args.env.items():
        globals()[k] = v
    total_steps = max_epochs * max_episodes * max_steps

    # preparations
    prep_dir_default(root_dir)
    logger.info("Directories prepared.")
    global_board.init(log_dir + "train_log")
    writer = global_board.writer

    actor = Actor(observe_dim, action_dim, 1).to(device)
    actor_t = Actor(observe_dim, action_dim, 1).to(device)
    critic = Critic(observe_dim, action_dim).to(device)
    critic_t = Critic(observe_dim, action_dim).to(device)
    critic2 = Critic(observe_dim, action_dim).to(device)
    critic2_t = Critic(observe_dim, action_dim).to(device)

    logger.info("Networks created")

    ddpg = DDPG_TD3(actor, actor_t, critic, critic_t, critic2, critic2_t,
                t.optim.Adam, nn.MSELoss(reduction='sum'), device,
                discount=0.99,
                update_rate=0.005,
                batch_size=ddpg_update_batch_size,
                learning_rate=0.001,
                replay_size=replay_size)

    if not restart:
        ddpg.load(root_dir + "/model", save_map)
    logger.info("DDPG framework initialized")

    # training
    # preparations
    env = BipedalWalker()

    # begin training
    # epoch > episode
    epoch = Counter()
    episode = Counter()
    episode_finished = False
    global_step = Counter()
    local_step = Counter()
    while epoch < max_epochs:
        epoch.count()
        logger.info("Begin epoch {}".format(epoch))
        while episode < max_episodes:
            episode.count()
            logger.info("Begin episode {}, epoch={}".format(episode, epoch))

            # render configuration
            if episode.get() % profile_int == 0 and global_step.get() > ddpg_warmup_steps:
                render = True
            else:
                render = False
            frames = []

            # model serialization
            if episode.get() % model_save_int == 0:
                ddpg.save(model_dir, save_map, episode.get() + (epoch.get() - 1) * max_episodes)
                logger.info("Saving model parameters, epoch={}, episode={}"
                            .format(epoch, episode))

            # batch size = 1
            episode_begin = time.time()
            total_reward = t.zeros([1, agent_num], device=device)
            actions = t.zeros([1, agent_num * 4], device=device)
            state, reward = t.tensor(env.reset(), dtype=t.float32, device=device), 0

            while not episode_finished and local_step.get() <= max_steps:
                global_step.count()
                local_step.count()

                step_begin = time.time()
                with t.no_grad():
                    old_state = state

                    # agent model inference

                    for ag in range(agent_num):
                        if not render:
                            actions[:, ag * 4: (ag + 1) * 4] = ddpg.act_with_noise(
                                {"state": state[ag * 24: (ag + 1) * 24].unsqueeze(0)},
                                explore_noise_params, mode="normal")
                        else:
                            actions[:, ag * 4: (ag + 1) * 4] = ddpg.act(
                                {"state": state[ag * 24: (ag + 1) * 24].unsqueeze(0)})

                    actions = t.clamp(actions, min=-1, max=1)
                    state, reward, episode_finished, _ = env.step(actions[0].to("cpu"))

                    if render:
                        frames.append(env.render(mode="rgb_array"))

                    state = t.tensor(state, dtype=t.float32, device=device)
                    reward = t.tensor(reward, dtype=t.float32, device=device).unsqueeze(dim=0)

                    total_reward += reward


                    for ag in range(agent_num):
                        ddpg.store_observe({"state": {"state": old_state[ag * 24: (ag + 1) * 24].unsqueeze(0).clone()},
                                            "action": {"action": actions[:, ag * 4:(ag+1)*4].clone()},
                                            "next_state": {"state": state[ag * 24: (ag + 1) * 24].unsqueeze(0).clone()},
                                            "reward": float(reward[ag]),
                                            "terminal": episode_finished or local_step.get() == max_steps})

                    writer.add_scalar("action_min", t.min(actions), global_step.get())
                    writer.add_scalar("action_mean", t.mean(actions), global_step.get())
                    writer.add_scalar("action_max", t.max(actions), global_step.get())

                step_end = time.time()

                writer.add_scalar("step_time", step_end - step_begin, global_step.get())
                writer.add_scalar("episodic_reward", t.mean(reward), global_step.get())
                writer.add_scalar("episodic_sum_reward", t.mean(total_reward), global_step.get())
                writer.add_scalar("episode_length", local_step.get(), global_step.get())

                logger.info("Step {} completed in {:.3f} s, epoch={}, episode={}".
                            format(local_step, step_end - step_begin, epoch, episode))

            logger.info("Sum reward: {}, epoch={}, episode={}".format(
                t.mean(total_reward), epoch, episode))

            if global_step.get() > ddpg_warmup_steps:
                for i in range(local_step.get()):
                    ddpg_train_begin = time.time()
                    ddpg.update(update_policy=i % 2 == 0, update_targets=i % 2 == 0)
                    ddpg_train_end = time.time()
                    logger.info("DDPG train Step {} completed in {:.3f} s, epoch={}, episode={}".
                                format(i, ddpg_train_end - ddpg_train_begin, epoch, episode))

            if render:
                create_gif(frames, "{}/log/images/{}_{}".format(root_dir, epoch, episode))

            local_step.reset()
            episode_finished = False
            episode_end = time.time()
            logger.info("Episode {} completed in {:.3f} s, epoch={}".
                        format(episode, episode_end - episode_begin, epoch))

        episode.reset()
