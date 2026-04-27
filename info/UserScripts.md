# User Scripts Reference

All standalone scripts in the project, organized by purpose.

## Overview

| Category | Script | Purpose |
|---|---|---|
| **GUI** | `launch.py` | Launch the Slippi AI desktop app |
| **Setup** | `runs/env.sh` | Shared environment config (sourced by run scripts) |
| | `scripts/repo_setup.sh` | One-time git setup after cloning |
| **Train: IL** | `scripts/train.py` | Imitation learning from Slippi replays |
| | `scripts/imitation_example.sh` | Example IL config (Fox, delay 18) |
| | `scripts/imitation_profiling.sh` | Quick IL profiling on toy dataset |
| **Train: Q** | `scripts/train_q.py` | Q-learning trainer |
| **Train: RL** | `scripts/rl_example.sh` | Example RL config with scaling guidance |
| | `runs/train_ganondorf.sh` | Single-agent RL: Ganon vs frozen medium-v2 |
| | `runs/train_ganondorf_vs_fox_two_fresh.sh` | Two-agent RL from scratch: Ganon vs Fox |
| | `runs/train_ganondorf_vs_multi_two.sh` | Two-agent RL: Ganon vs all 12 top-tiers |
| | `runs/train_pawl_against_one.sh` | Two-agent RL: Ganon vs one specific character |
| | `rl_vs_mediumv2.sh` | RL: Ganon vs frozen medium-v2 (multi-char) |
| **Evaluate** | `scripts/eval_two.py` | Match between two agents or AI vs human |
| | `scripts/run_evaluator.py` | Evaluate agent in game environment |
| | `scripts/eval_names.py` | Compare performance across player names |
| | `runs/eval_ganondorf.sh` | Evaluate Ganon RL checkpoint vs top-12 |
| **Netplay** | `scripts/netplay.py` | AI vs human via Slippi Online |
| | `scripts/twitchbot.py` | Twitch bot: viewers play vs AI on stream |
| **Tech Tests** | `runs/run_compare.sh` | Compare tech skill at different delays |
| | `scripts/compare_local_vs_netplay.py` | Detailed tech skill metrics by delay |
| | `runs/run_multishine_test.sh` | Multishine consistency across delays |
| | `scripts/multishine_delay_test.py` | Multishine test (Python version) |
| | `scripts/run_multishine.py` | Frame-perfect multishine for inspection |
| | `scripts/test_inputs.py` | Validate controller input/output matching |
| | `scripts/test_ffw.py` | Test char/stage fast-forward compatibility |
| **Profiling** | `scripts/run_dolphin.py` | Benchmark Dolphin throughput |
| | `scripts/profile_dolphin.py` | Profile Dolphin with Ray (multi-instance) |
| | `scripts/profile_data.py` | Profile data loading performance |
| **Utilities** | `fix_rl_checkpoint.py` | Fix mismatched char/name lists in checkpoints |
| | `view_pkl.py` | Inspect pickle files as JSON |
| | `scripts/strip_models.py` | Strip models to policy-only for inference |
| | `scripts/update_delay.py` | Update training delay in RL checkpoint |
| | `scripts/compress_replays_7z.py` | Archive replays with 7-Zip |
| | `scripts/create_model.py` | Create test models for testing |
| | `scripts/update_toy_dataset.sh` | Regenerate toy dataset from replays |
| **Infra** | `lint.sh` | Run pylint on codebase |
| | `scripts/xvfb.sh` | Start virtual X display server |
| | `scripts/stream.sh` | Stream display to Twitch via FFmpeg |
| | `scripts/omniboard.sh` | Start Omniboard experiment dashboard |
| **Tests** | `tests/training_test.sh` | IL sanity check (~10s) |
| | `tests/train_rl.sh` | RL sanity check (10 steps) |
| | `tests/train_two.sh` | Two-agent RL sanity check (10 steps) |
| | `tests/train_q.sh` | Q-learning sanity check (~10s) |
| | `tests/run_evaluator.sh` | Evaluator sanity check (fake envs) |

---

## GUI Launcher

### `launch.py`
Entry point for the Slippi AI GUI application. Just run `python launch.py`.

---

## Environment Setup

### `runs/env.sh`
Shared environment setup sourced by all run scripts. Detects platform (Linux/WSL/Windows), sets paths for Melee ISO, Dolphin, and agents directory, and applies hardware optimization flags. Supports local overrides via `env.local.sh`.

### `scripts/repo_setup.sh`
One-time repository setup after cloning. Configures local git settings.

---

## Training - Imitation Learning

### `scripts/train.py`
Train a model using imitation learning from Slippi replays.
```
python scripts/train.py --config.* --wandb.project=slippi-ai
```

### `scripts/imitation_example.sh`
Example imitation learning config for Fox with delay 18. 3-layer transformer, batch size 512, 6-day runtime. Modify for custom training.

### `scripts/imitation_profiling.sh`
Quick profiling run using the toy dataset. 30-second runtime with wandb disabled. Useful for validating the training pipeline.

---

## Training - Q-Learning

### `scripts/train_q.py`
Q-learning trainer. Configuration via dataclass-based flags.
```
python scripts/train_q.py --config.*
```

---

## Training - Reinforcement Learning

### `scripts/rl_example.sh`
Example RL training config with optimized hyperparameters for i7-11700K + RTX 3080Ti + 64GB. Contains scaling guidance: increase `num_envs` until RAM is full (~40MB per env), then increase `rollout_length`.

### `runs/train_ganondorf.sh`
Single-agent RL: Ganondorf vs frozen medium-v2 opponent. Default 6-day runtime. Kills zombie processes before launching.
```
./runs/train_ganondorf.sh
./runs/train_ganondorf.sh --config.runtime.max_runtime=3600
```

### `runs/train_ganondorf_vs_fox_two_fresh.sh`
Two-agent RL from scratch: Ganondorf vs Fox (top-12, unfrozen). Both agents learn simultaneously. Default 6-day runtime.

### `runs/train_ganondorf_vs_multi_two.sh`
Two-agent RL: Ganondorf vs all 12 top-tier characters. Opponent cycles through characters (60 envs / 12 chars = 5 per matchup). Default 6-day runtime.

### `runs/train_pawl_against_one.sh`
Two-agent RL: Ganondorf vs one specific top-12 character. Resumes from previous checkpoint with configurable opponent character and name.

### `rl_vs_mediumv2.sh`
Root-level script for training Ganondorf RL vs frozen medium-v2 with multi-character opponents. Default 6-day runtime.

---

## Evaluation

### `scripts/eval_two.py`
Run a match between two trained agents, or AI vs human.
```
python scripts/eval_two.py --p1.path=<model> --p2.path=<model> --dolphin.* --num_games=3
```

### `scripts/run_evaluator.py`
Evaluate a trained agent in game environments.
```
python scripts/run_evaluator.py --player.* --opponent.* --rollout_length=3600 --num_envs=1
```
Supports `--fake_envs` for testing without Dolphin.

### `scripts/eval_names.py`
Compare performance across different player names in a model.
```
python scripts/eval_names.py --num_names=4 --names=NAME1,NAME2
```

### `runs/eval_ganondorf.sh`
Evaluate Ganondorf RL checkpoint vs top-12 opponent. Reports KO diff per minute and FPS.
```
./runs/eval_ganondorf.sh
WATCH=1 ./runs/eval_ganondorf.sh              # with GUI
./runs/eval_ganondorf.sh --opponent.character=MARTH
```

---

## Netplay / Online Play

### `scripts/netplay.py`
Run AI agent against a human player via Slippi Online netplay.
```
python scripts/netplay.py --agent.path=<model> --char=CHARACTER \
    --dolphin.connect_code=<CODE> --dolphin.user_json_path=<path>
```
Auto-computes `online_delay` from the model's trained delay with 1 frame headroom.

### `scripts/twitchbot.py`
Twitch bot that lets viewers play against the AI on stream. Supports multi-session streaming via Ray.
```
python scripts/twitchbot.py --token=<twitch_token> --channel=<channel> \
    --bot=<model_path> --max_sessions=2 --stream
```

---

## Tech Skill / Delay Testing

### `runs/run_compare.sh`
Compare tech skill execution (wavedashes, L-cancels) at different online delays.
```
./runs/run_compare.sh
WATCH=1 ./runs/run_compare.sh --delays=0,2,3 --num_games=2
```

### `scripts/compare_local_vs_netplay.py`
Python equivalent of `run_compare.sh`. Detailed tech skill metrics including input mismatch rates, wavedash quality, and L-cancel success.
```
python scripts/compare_local_vs_netplay.py --agent.path=<model> --delays=0,2,3 --num_games=3
```

### `runs/run_multishine_test.sh`
Test multishine consistency across different online delays.
```
./runs/run_multishine_test.sh --delays=0,1,2,3,4 --runtime=30
```

### `scripts/multishine_delay_test.py`
Python multishine delay test. Reports shine counts, jump counts, and success rates per delay.
```
python scripts/multishine_delay_test.py --delays=0,1,2,3,4 --runtime=30
```

### `scripts/run_multishine.py`
Run frame-perfect multishine inputs with Fox on both ports. Outputs state logs for manual inspection.

### `scripts/test_inputs.py`
Test that controller inputs match observed outputs in game. Validates triggers, analog sticks, and digital buttons.
```
python scripts/test_inputs.py --dolphin.* --debug
```

### `scripts/test_ffw.py`
Test character/stage combinations in fast-forward mode. Outputs compatibility matrix as JSON.
```
python scripts/test_ffw.py --output=results.json
```

---

## Profiling / Benchmarking

### `scripts/run_dolphin.py`
Benchmark Dolphin emulator throughput with CPU vs CPU.
```
python scripts/run_dolphin.py --N=4 --frames=3600 --render --overclock=1.5
```

### `scripts/profile_dolphin.py`
Profile Dolphin performance with multiple instances via Ray.
```
python scripts/profile_dolphin.py --N=4 --frames=3600 --chunk_size=10
```

### `scripts/profile_data.py`
Profile data loading performance for training pipeline.
```
python scripts/profile_data.py --data_dir=<path> --batch_size=32 --runtime=5
```

---

## Utilities

### `fix_rl_checkpoint.py`
Fix RL checkpoint files with mismatched opponent character/name lists. Interactive menu.
```
python fix_rl_checkpoint.py <checkpoint.pkl>
```

### `view_pkl.py`
Inspect pickle files and convert to JSON for analysis.
```
python view_pkl.py <file.pkl>
```

### `scripts/strip_models.py`
Strip models down to policy-only weights for inference (removes training-only weights).
```
python scripts/strip_models.py --src=pickled_models --dst=stripped_models
```

### `scripts/update_delay.py`
Update the training delay in an RL checkpoint and adjust the teacher model path.
```
python scripts/update_delay.py --path=<experiment_dir> --delay=21
```

### `scripts/compress_replays_7z.py`
Archive replay files using 7-Zip, excluding currently open files.
```
python scripts/compress_replays_7z.py --source_dir=Replays/ --zip_filename=Replays.zip
```

### `scripts/create_model.py`
Create and save test models with custom configurations. Used for unit/integration testing.

### `scripts/update_toy_dataset.sh`
Regenerate the toy dataset from raw replay files. Parses replays, creates dataset, and renames directories.

---

## Infrastructure

### `lint.sh`
Run pylint on `slippi_ai/`, `slippi_db/`, `scripts/`, and `tests/`.

### `scripts/xvfb.sh`
Start a virtual X framebuffer display server on display :99. Run before `stream.sh`.

### `scripts/stream.sh`
Stream X display :99 to Twitch using FFmpeg with NVIDIA hardware encoding. Requires `xvfb.sh` running first.

### `scripts/omniboard.sh`
Start Omniboard MongoDB dashboard for experiment visualization.

---

## Test Runners

Short integration/sanity-check scripts in `tests/`:

| Script | What it tests | Duration |
|---|---|---|
| `tests/training_test.sh` | IL training on toy dataset | ~10 seconds |
| `tests/train_rl.sh` | RL training (self-play, fake envs) | 10 steps |
| `tests/train_two.sh` | Two-agent RL training (fake envs) | 10 steps |
| `tests/train_q.sh` | Q-learning training | ~10 seconds |
| `tests/run_evaluator.sh` | Evaluator with fake environments | ~180 frames |
