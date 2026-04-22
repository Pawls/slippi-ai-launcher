# Slippi-AI Launcher - Claude Code Guide

**If you're here for the bot/Discord/netplay work, read [AGENTS.md](AGENTS.md) first.** This file covers the ML training side.

## Project Overview
Fork of [slippi-ai](https://github.com/vladfi1/slippi-ai) extended with a full-featured GUI launcher and Rust-accelerated reward computation. Two-stage ML pipeline: imitation learning (IL) from Slippi replays, then reinforcement learning (RL) via self-play using PPO. The launcher provides a desktop app for training, evaluating, and playing against AI agents in Super Smash Bros. Melee.

## Architecture

### GUI Launcher (`LAUNCHER/`)
- **Entry point**: `launch.py` → `LAUNCHER/slippi_launcher.py`
- **Framework**: tkinter with Navigator/Screen pattern (`LAUNCHER/screens/__init__.py`)
- **Config**: INI-based (`slippi_gui_config.ini`), auto-detects Slippi paths on Windows
- **Data stores**: JSON-backed (agent_store, match_store, tournament_store, replay_store, resource_store)
- **Screens**: home, setup, settings, play, train_il, train_rl, evaluate, agents, replays, history, tournament, resources, config_diff, create, log_viewer, train_help

### ML Training
- **Imitation learning**: `scripts/train.py`
- **Q-learning**: `scripts/train_q.py`
- **RL (single agent)**: `slippi_ai/rl/run.py`
- **RL (two-agent)**: `slippi_ai/rl/train_two.py` (primary training mode)
- **Evaluation**: `scripts/eval_two.py`

### Rust Native Extensions (`slippi_native/`)
- PyO3 + maturin crate providing `voluntary_death_forward_fill()` for reward computation
- Python fallback retained if crate not installed
- Build: `cd slippi_native && ./build.sh`
- Type stub at `slippi_native/slippi_native.pyi`

## Training Scripts
All training launch scripts live in `runs/`. Source `runs/env.sh` first for environment setup.
- `runs/train_ganondorf_vs_multi_two.sh` - Ganondorf vs top12 multi-char (train_two)
- `runs/train_ganondorf.sh` - Single-agent RL vs frozen opponent
- `runs/train_pawl_against_one.sh` - Single-character opponent training
- `runs/eval_ganondorf.sh` - Evaluation

## Experiment Tracking
- **wandb project**: `slippi-ai`
- **wandb group**: `rl-ganondorf` (for Ganondorf RL runs)
- Logs: `wandb/run-*/files/output.log`

## Checkpoint Naming Convention
Format: `{label}_delay_{frames}_vs_{opp_label}-{port}.pkl`
- `label` overrides character name in filename (set via `--config.p1.label` or `--config.p2.label`)
- Without label, uses character name (lowercase) or `multi` for multi-char agents
- Logic in `slippi_ai/rl/train_two_lib.py:set_opponent()` (~line 213)

## Experiment Directory
- `experiments/train_two/` - Two-agent RL checkpoints
- `experiments/rl/` - Single-agent RL checkpoints
- `agents/` - Pre-trained IL models

## Key Config Flags (train_two)
- `--config.p1.label` / `--config.p2.label` - Override checkpoint name prefix
- `--config.p2.char=FOX,MARTH,...` - Opponent character(s), comma-separated
- `--config.p2.restore=<path>` - Restore from checkpoint (skips burnin)
- `--config.learner.kl_teacher_weight` - KL penalty to IL teacher
- `--config.learner.policy_gradient_weight` - PPO policy gradient weight
- `--config.actor.num_envs` - Number of parallel environments
- Boolean flags: use `--flag=False`, not `--noflag` (abseil/fancyflags)

## Hardware
- RTX 3090, 16 logical cores, 64GB RAM
- WSL2 (Linux on Windows) for headless training
- GUI targets Windows (launches from Windows side)
- ~840 FPS with 120 envs

## Style & Conventions
- TensorFlow 2 + Sonnet (snt) for models
- `tf.function` with `jit_compile` for compiled training steps
- Time-major tensors in training (swap axes from batch-major)
- `embed.StateAction` for state/action representation
- GUI screens follow the Navigator/Screen pattern — new screens need registration in `LAUNCHER/app.py`
- Data stores use JSON persistence with the pattern in `LAUNCHER/match_store.py`

## Important Gotchas
- Rust extensions are loaded at import time. Rebuilding `slippi_native` mid-training won't affect the running process, but will affect the next run. Ensure Rust functions handle the same array shapes as the Python fallback (can be 1D, 2D, or 3D depending on batching).
- Local play should always use `online_delay=2` to match Slippi Online conditions. Netplay auto-computes delay from the model.
- Editing project files during training does not affect the running process (Python loads modules at startup), but reinstalling native extensions or modifying files between `import` and use could cause issues on restart.
