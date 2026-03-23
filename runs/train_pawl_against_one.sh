#!/usr/bin/env bash

# Train Ganondorf (RL) vs Fox (top12 IL, unfrozen) using train_two.
# Both agents learn simultaneously — the Fox opponent adapts to Ganondorf.

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# Player 1: Ganondorf — restore from previous run, teacher is the original IL model
P1_RESTORE="$PROJECT_ROOT/experiments/train_two/ganon_d18_vs_multi_fox_d21_run2/ganondorf_delay_18_vs_top12chars-1.pkl"
P1_TEACHER="$PROJECT_ROOT/agents/pawl_ganon_imitation_v1.pkl"
P1_NAME="PAWL#723"

# Player 2: top12 imitation model, delay 21 (character/name can be changed between runs)
P2_RESTORE="$PROJECT_ROOT/experiments/train_two/ganon_d18_vs_multi_fox_d21_run2/top12chars_delay_21_vs_ganondorf-2.pkl"
P2_TEACHER="$PROJECT_ROOT/agents/top12_d21_imitation_3x768_v5.pkl"
P2_NAME="Platinum Player"

# Training parameters
NUM_DAYS=6
RUNTIME=$(($NUM_DAYS * 24 * 60 * 60))
TAG=ganon_d18_v_top12_d21_run3

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
  --config.dolphin.path="$DOLPHIN_HEADLESS" \
  --config.dolphin.iso="$MELEE_ISO" \
  --config.dolphin.console_timeout=60 \
  --config.p1.restore="$P1_RESTORE" \
  --config.p1.teacher="$P1_TEACHER" \
  --config.p1.name="$P1_NAME" \
  --config.p1.batch_steps=4 \
  --config.p2.restore="$P2_RESTORE" \
  --config.p2.teacher="$P2_TEACHER" \
  --config.p2.name="$P2_NAME" \
  --config.p2.char=PEACH \
  --config.p2.label="top12chars" \
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
  --config.learner1.reward.l_cancel_miss_penalty=0 \
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
