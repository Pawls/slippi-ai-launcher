# Slippi-AI Launcher

A desktop application for training, evaluating, and playing against AI agents in Super Smash Bros. Melee. Fork of [slippi-ai (Phillip II)](https://github.com/vladfi1/slippi-ai) with a full GUI launcher and performance optimizations.

There is a [discord channel](https://discord.gg/hfVTXGu) for discussion/feedback/support.

## Features

- **Play vs AI** — local play against trained agents with automatic delay configuration
- **Training Management** — launch and monitor imitation learning (IL) and reinforcement learning (RL) runs with live log viewing
- **Agent Library** — browse, nickname, compare, and manage trained models with metadata
- **Replay Browser** — view training replays with metadata extraction (character, stage, players)
- **Tournament Mode** — round-robin matchups between agents with leaderboard tracking
- **Match History** — track all games played with per-agent and per-character statistics
- **Resource Monitor** — live GPU/CPU/RAM stats during training
- **Config Diff** — compare training configurations across runs

## Quick Start

Download or `git clone` this repository. From the repository root:

**Windows:** double-click `setup.bat` (or run it in a terminal).
**Linux / WSL / macOS:** `./setup.sh`

That creates `.venv`, installs pip, and installs this package and all its
dependencies in editable mode. If you prefer to do it by hand:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    |    Unix/WSL: source .venv/bin/activate
pip install --upgrade pip
pip install -e .        # pulls everything declared in setup.cfg

# Launch the GUI
python launch.py

# Or run directly from the command line
python scripts/eval_two.py \
  --dolphin.iso <path/to/ssbm.iso> \
  --p1.type human \
  --p2.ai.path <path/to/trained/model> \
  [--dolphin.copy_home_directory]
```

A model capable of playing 12 different characters is available [here](https://www.dropbox.com/scl/fi/lpi9krfei1knfvfw7up7v/medium-v2?rlkey=qmah3qfz5anwva93x48zcx01k&st=sxo8hbeb&dl=0). You can change the character by setting `--p2.character <fox/falco/marth/...>`.

### Optional: Rust Native Extensions

For faster reward computation during RL training:

```bash
# Requires Rust toolchain and maturin
cd slippi_native && ./build.sh
```

### Notes
* Tested with Python 3.10, 3.11, and 3.13.
* The GUI auto-detects Slippi Dolphin paths on Windows. On other platforms, configure paths in Settings.
* By default, human players use Wii-U controller adapters. Pass `--dolphin.copy_home_directory` to use your own Dolphin controller config.
* On Windows you may need to [enable long paths](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation?tabs=powershell#registry-setting-to-enable-long-paths) for pip installs.

## Training Pipeline

Phillip uses a two-stage training pipeline:

1. **Imitation Learning** — learn to play from a dataset of human Slippi replays (`scripts/train.py`)
2. **Reinforcement Learning** — refine the imitation policy via self-play with PPO (`slippi_ai/rl/train_two.py`)

Training scripts live in `runs/`. Source `runs/env.sh` first for environment setup. Metrics are logged to [wandb](https://wandb.ai/).

## Recordings

Phillip has played a number of top players:
* [Zain 1](https://www.youtube.com/watch?v=c8nRFAGvr2c), [Zain 2](https://www.youtube.com/watch?v=XBHaHlC3_p4)
* [Amsa + Cody](https://www.youtube.com/watch?v=WGsN7lWBQP)
* [Moky](https://www.youtube.com/watch?v=1kviVflqXc4)
* [Aklo](https://www.youtube.com/watch?v=OGOEqhMptq0)

## Acknowledgements

* Huge thanks to Fizzi for Slippi, the fast-forward gecko code for RL training, and providing imitation training data via anonymized ranked collections.
* Big thanks to [altf4](https://github.com/altf4) for [libmelee](https://github.com/altf4/libmelee), making melee AI development accessible to everyone.
* Thank you to the many players who have generously shared their replays.
* Thank you to my dad for providing the computing hardware used to train Phillip.
