#!/usr/bin/env bash

# Train Ganondorf (RL, single-learner) vs medium-v2 opponent (frozen).
# Uses slippi_ai/rl/run.py — the single-agent PPO trainer.
#
# Usage:
#   ./runs/train_ganondorf.sh                         # defaults: 6 days
#   ./runs/train_ganondorf.sh --config.runtime.max_runtime=3600  # 1 hour

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# Paths
AGENT_PATH="$PROJECT_ROOT/experiments/rl/ganondorf_d18_rl_vs_mediumv2_run3/latest.pkl"
OPPONENT_PATH="$AGENTS_DIR/medium-v2"

# Training parameters
NUM_DAYS=6
RUNTIME=$(($NUM_DAYS * 24 * 60 * 60))

CHAR=ganondorf
NAME="PAWL#723"
DELAY="18"
TAG=${CHAR}_d${DELAY}_rl_vs_mediumv2_run4

# KILL ZOMBIES FIRST
killall -9 AppRun.Wrapped 2>/dev/null
killall -9 dolphin-emu 2>/dev/null
killall -9 Slippi_Netplay_Mainline_ExiAI_NoLeak-x86_64.AppImage 2>/dev/null

python slippi_ai/rl/run.py \
  --config.runtime.tag="$TAG" \
  --config.runtime.max_step=10000000 \
  --config.runtime.max_runtime=$RUNTIME \
  --config.runtime.log_interval=300 \
  --config.runtime.save_interval=600 \
  --config.dolphin.infinite_time \
  --config.dolphin.headless \
  --config.dolphin.log_level=3 \
  --config.dolphin.log_types='' \
  --config.dolphin.path="$DOLPHIN_HEADLESS" \
  --config.dolphin.iso="$MELEE_ISO" \
  --config.dolphin.console_timeout=60 \
  --config.learner.learning_rate=3e-5 \
  --config.learner.value_cost=1 \
  --config.learner.reward.damage_ratio=0.01 \
  --config.learner.reward.ledge_grab_penalty=0.02 \
  --config.learner.reward.stalling_penalty=0.1 \
  --config.learner.reward.stalling_threshold=50.0 \
  --config.learner.reward.shield_break_penalty=0.5 \
  --config.learner.reward.offstage_death_penalty=0.6 \
  --config.learner.reward.wavedash_reward=0.005 \
  --config.learner.reward.l_cancel_miss_penalty=0 \
  --config.learner.reward_halflife=8.0 \
  --config.learner.reward.approaching_factor=0.003 \
  --config.learner.policy_gradient_weight=3 \
  --config.learner.kl_teacher_weight=5e-2 \
  --config.learner.reverse_kl_teacher_weight=5e-2 \
  --config.learner.ppo.num_epochs=2 \
  --config.learner.ppo.num_batches=16 \
  --config.learner.ppo.beta=3e-1 \
  --config.learner.ppo.epsilon=1e-2 \
  --config.learner.ppo.max_mean_actor_kl=1e-4 \
  --config.learner.ppo.minibatched=False \
  --config.teacher="$AGENT_PATH" \
  --config.actor.rollout_length=60 \
  --config.actor.num_envs=120 \
  --config.actor.inner_batch_size=12 \
  --config.actor.async_envs=True \
  --config.actor.num_env_steps=4 \
  --config.actor.gpu_inference=True \
  --config.agent.name="$NAME" \
  --config.agent.batch_steps=4 \
  --config.agent.char=GANONDORF \
  --config.opponent.type=other \
  --config.opponent.train=False \
  --config.opponent.other.path="$OPPONENT_PATH" \
  --config.opponent.other.char=FOX \
  --config.opponent.other.char=FALCO \
  --config.opponent.other.char=MARTH \
  --config.opponent.other.char=SHEIK \
  --config.opponent.other.char=JIGGLYPUFF \
  --config.opponent.other.char=CPTFALCON \
  --config.opponent.other.char=PEACH \
  --config.opponent.other.char=YOSHI \
  --config.opponent.other.char=POPO \
  --config.opponent.other.char=LUIGI \
  --config.opponent.other.char=PIKACHU \
  --config.opponent.other.char=SAMUS \
  --config.opponent.other.name="Master Player,Master Player,Master Player,Master Player,Master Player,Master Player,Master Player,Master Player,Master Player,Master Player,Master Player,Master Player" \
  --config.runtime.reset_every_n_steps=6144 \
  --config.runtime.burnin_steps_after_reset=5 \
  --config.optimizer_burnin_steps=128 \
  --config.value_burnin_steps=128 \
  --wandb.name="$TAG" \
  --wandb.mode=online \
  --wandb.project=slippi-ai \
  --wandb.group=rl-ganondorf \
  --wandb.tags=ppo \
  "$@"
