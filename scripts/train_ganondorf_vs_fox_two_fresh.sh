#!/usr/bin/env bash

# Train Ganondorf (RL) vs Fox (top12 IL, unfrozen) using train_two.
# Fresh start from the original imitation model — no RL checkpoint restore.

cd /home/pawl/melee/slippi-ai-launcher

# --- HARDWARE OPTIMIZATION FLAGS ---
export OMP_NUM_THREADS=1
export TF_ENABLE_ONEDNN_OPTS=1
export TMPDIR=/dev/shm

# Paths
ISO_PATH="/home/pawl/melee/melee.iso"
DOLPHIN_PATH="/home/pawl/melee/dolphin-ai/squashfs-root/AppRun"

# Player 1: Ganondorf (original imitation model — fresh RL start)
P1_TEACHER="/home/pawl/melee/slippi-ai-launcher/agents/pawl_ganon_imitation_v1.pkl"
P1_NAME="PAWL#723"

# Player 2: Fox (top12 imitation model, delay 21)
P2_TEACHER="/home/pawl/melee/slippi-ai-launcher/agents/top12_d21_imitation_3x768_v5.pkl"
P2_NAME="Master Player"

# Training parameters
NUM_DAYS=6
RUNTIME=$(($NUM_DAYS * 24 * 60 * 60))
TAG=ganon_d18_vs_fox_d21_fresh_run1

# KILL ZOMBIES FIRST
killall -9 AppRun.Wrapped 2>/dev/null
killall -9 dolphin-emu 2>/dev/null
killall -9 Slippi_Netplay_Mainline_ExiAI_NoLeak-x86_64.AppImage 2>/dev/null

python slippi_ai/rl/train_two.py \
  --config.runtime.tag="$TAG" \
  --config.runtime.max_step=10000000 \
  --config.runtime.max_runtime=$RUNTIME \
  --config.runtime.log_interval=300 \
  --config.runtime.save_interval=600 \
  --config.runtime.reset_every_n_steps=6144 \
  --config.runtime.burnin_steps_after_reset=5 \
  --config.dolphin.infinite_time \
  --config.dolphin.headless \
  --config.dolphin.log_level=3 \
  --config.dolphin.log_types='' \
  --config.dolphin.path="$DOLPHIN_PATH" \
  --config.dolphin.iso="$ISO_PATH" \
  --config.dolphin.console_timeout=60 \
  --config.p1.teacher="$P1_TEACHER" \
  --config.p1.name="$P1_NAME" \
  --config.p1.batch_steps=4 \
  --config.p2.teacher="$P2_TEACHER" \
  --config.p2.name="$P2_NAME" \
  --config.p2.char=FOX \
  --config.p2.batch_steps=4 \
  --config.learner.learning_rate=3e-5 \
  --config.learner.value_cost=1 \
  --config.learner.reward.damage_ratio=0.01 \
  --config.learner.reward_halflife=8.0 \
  --config.learner.policy_gradient_weight=3 \
  --config.learner.kl_teacher_weight=5e-2 \
  --config.learner.reverse_kl_teacher_weight=5e-2 \
  --config.learner.ppo.num_epochs=2 \
  --config.learner.ppo.num_batches=16 \
  --config.learner.ppo.beta=3e-1 \
  --config.learner.ppo.epsilon=1e-2 \
  --config.learner.ppo.max_mean_actor_kl=1e-4 \
  --config.learner.ppo.minibatched=False \
  --config.learner1.reward.ledge_grab_penalty=0.02 \
  --config.learner1.reward.stalling_penalty=0.1 \
  --config.learner1.reward.stalling_threshold=50.0 \
  --config.learner1.reward.approaching_factor=0.000 \
  --config.learner1.reward.l_cancel_miss_penalty=0.05 \
  --config.learner1.reward.offstage_death_penalty=0.5 \
  --config.learner2.learning_rate=1e-5 \
  --config.actor.rollout_length=60 \
  --config.actor.num_envs=120 \
  --config.actor.inner_batch_size=12 \
  --config.actor.async_envs=True \
  --config.actor.num_env_steps=4 \
  --config.actor.gpu_inference=True \
  --config.optimizer_burnin_steps=128 \
  --config.value_burnin_steps=128 \
  --wandb.name="$TAG" \
  --wandb.mode=online \
  --wandb.project=slippi-ai \
  --wandb.group=rl-ganondorf \
  --wandb.tags=ppo,train_two \
  "$@"
