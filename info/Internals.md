# Project Internals

How the codebase fits together. Covers every library module, FastAPI route, and data pipeline file.

## Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────┐
│                   LAUNCHER (FastAPI backend)                │
│  api/ (routes: play, agents, training, replays, bot, ...)   │
│  config.py (path detection, model metadata, persistence)    │
│  Consumed by ../slippi-ai-gui (Tauri/Svelte) and            │
│              ../caught-slippin (Discord bot)                │
└──────────────────────────┬──────────────────────────────────┘
                           │ launches scripts & modules
┌──────────────────────────┴──────────────────────────────────┐
│                    SLIPPI_AI (Learning)                     │
│                                                             │
│  Imitation Learning                                         │
│    data.py → embed.py → networks.py → policies.py           │
│    → learner.py → saving.py                                 │
│                                                             │
│  Q-Learning                                                 │
│    data.py → q_learner.py → q_function.py                   │
│                                                             │
│  Reinforcement Learning (rl/)                               │
│    envs.py ↔ dolphin.py    (Dolphin interaction)            │
│    evaluators.py → trajectories → rl/learner.py  (PPO)      │
│    value_function.py       (advantage estimation)           │
│    reward.py, mirror.py    (state preprocessing)            │
│                                                             │
│  Core: types.py, observations.py, controller_lib.py,        │
│        eval_lib.py, saving.py                               │
└──────────────────────────┬──────────────────────────────────┘
                           │ consumes parsed data
┌──────────────────────────┴──────────────────────────────────┐
│                    SLIPPI_DB (Parsing)                      │
│  parse_local.py (orchestrator)                              │
│    → parse_peppi.py / parse_libmelee.py (replay parsing)    │
│    → preprocessing.py (Arrow schema)                        │
│    → parsing_utils.py (parquet serialization)               │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow

1. **Dataset Creation**: `.slp` replays → `parse_local.py` → peppi/libmelee parsers → Arrow arrays → parquet + SQLite
2. **Imitation Learning**: parquet → `data.py` (batching) → `embed.py` (featurize) → `networks.py` → `policies.py` → `learner.py` (gradient step) → `saving.py`
3. **Q-Learning**: Same data path, but trains a Q-function for action values alongside the policy
4. **RL Training**: `evaluators.py` spawns rollout workers → `envs.py` steps Dolphin → `controller_lib.py` sends inputs → trajectories fed to `rl/learner.py` (PPO) → `saving.py`
5. **Evaluation**: `eval_lib.py` loads agents → runs them in Dolphin → reports metrics (KO diff, tech skill, FPS)

---

## Module Reference

### slippi_ai/ - Main Learning Library

#### Core Data & Types

| File | Purpose | Key Exports |
|---|---|---|
| `types.py` | Central type definitions for game state (Arrow StructArrays + NamedTuples) | `Game`, `Player`, `Controller`, `Buttons`, `Stick`, `GAME_TYPE` |
| `paths.py` | Project path constants | `PACKAGE_PATH`, `DATA_PATH`, `DEMO_CHECKPOINT`, `TOY_DATASET` |
| `data.py` | Dataset loading, batching, and window slicing from parquet files | `DataSource`, `Batch`, `Frames`, `make_source()`, `toy_data_source()` |
| `nametags.py` | Player identification from Slippi metadata | `name_from_metadata()` |

#### Observations & Preprocessing

| File | Purpose | Key Exports |
|---|---|---|
| `observations.py` | Filters and transforms raw game state before policy input (animation cancellation, tech tracking) | `ObservationFilter`, `AnimationFilter`, `TechTrackingFilter`, `build_observation_filter()` |
| `reward.py` | Per-frame reward computation for RL (damage, deaths, approach distance, tech skill bonuses) | `compute_rewards()`, `is_dying()`, `detect_l_cancel_misses()`, `grabbed_ledge()` |
| `replay_stats.py` | Per-replay statistics for dataset quality filtering (conversions, neutral wins, tech skill %) | Various counting/detection functions |
| `mirror.py` | Left/right mirroring of game states for data augmentation | `mirror_game()`, `mirror_player()`, `mirror_controller()` |

#### Network Architecture

| File | Purpose | Key Exports |
|---|---|---|
| `networks.py` | Recurrent network cores (LSTM, GRU, MLP) with `initial_state`, `step`, and `unroll` methods | `Network` (base), `construct_network()`, `CONSTRUCTORS` registry |
| `embed.py` | Embedding layers that convert nested game state structures into flat tensors for network input | `Embedding`, `StructEmbedding`, `make_game_embedding()`, `get_state_action_embedding()` |
| `controller_heads.py` | Output heads that convert network outputs to controller action distributions | `ControllerHead`, `Independent`, `SampleOutputs`, `DistanceOutputs` |

#### Policies & Value Functions

| File | Purpose | Key Exports |
|---|---|---|
| `policies.py` | End-to-end policy: embeddings → network → controller head. Takes game state, outputs actions | `Policy`, `UnrollOutputs` |
| `value_function.py` | Predicts expected returns from state-action pairs for advantage estimation | `ValueFunction`, `ValueOutputs`, `FakeValueFunction` |
| `q_function.py` | Estimates state-action values for Q-learning | `QFunction`, `QOutputs` |

#### Trainers

| File | Purpose | Key Exports |
|---|---|---|
| `learner.py` | Imitation learning (behavioral cloning) gradient computation | `Learner`, `LearnerConfig` |
| `q_learner.py` | Q-learning trainer: trains Q-function + sample/target policies via TD learning | `Learner` (QLearner), `LearnerConfig` |
| `train_lib.py` | High-level IL training loop: data loading, loss computation, logging, checkpointing | `TrainManager`, training config dataclasses |
| `train_q_lib.py` | High-level Q-learning training loop | Training config, main run function |
| `rl_lib.py` | RL utilities (discounted return computation via backward scan) | `discounted_returns()` |

#### Environment & Agent Interaction

| File | Purpose | Key Exports |
|---|---|---|
| `envs.py` | Wraps Dolphin emulator as an RL environment (step, reset, batched execution) | `Environment`, `EnvOutput` |
| `dolphin.py` | Dolphin process management, controller I/O, menu navigation, version detection | `Player`, `Human`, `CPU`, `AI` |
| `controller_lib.py` | Converts between libmelee ControllerState and internal Controller type | `from_gamestate_controller()`, `send_controller()`, `to_raw_controller()` |
| `evaluators.py` | Rollout workers that collect game trajectories from environments for learning | `RolloutWorker`, `Trajectory` |
| `eval_lib.py` | Agent loading, inference wrappers, and environment setup for evaluation | `FakeAgent`, agent wrappers |

#### Utilities

| File | Purpose | Key Exports |
|---|---|---|
| `utils.py` | General helpers: profiling, nested structure operations, tree utilities | `batch_nest()`, `unstack_nest()`, `field()`, `Profiler` |
| `tf_utils.py` | TensorFlow utilities: RNN unrolling, tensor ops, tf.function compilation helpers | `dynamic_rnn()`, `where()`, `get_stats()`, `to_numpy()` |
| `flag_utils.py` | Converts dataclass configs to/from CLI flag definitions (for abseil/fancyflags) | `dataclass_from_dict()`, flag item construction |
| `saving.py` | Checkpoint serialization: policies, optimizers, configs to/from pickle | `save_policy_to_disk()`, `load_policy_from_disk()` |
| `techskill.py` | Hardcoded multishine combo for testing controller output | `MultiShine` |
| `unroll_agent.py` | Extracts full policy outputs over a replay for debugging/analysis | `unroll()` |

---

### slippi_ai/rl/ - Reinforcement Learning Module

| File | Purpose | Key Exports |
|---|---|---|
| `learner.py` | PPO trainer with KL divergence constraints and advantage estimation | `Learner`, `PPOConfig`, `LearnerConfig`, `LearnerOutputs` |
| `run_lib.py` | Single-agent RL training pipeline: rollouts, learning, logging | `Config`, `RuntimeConfig`, `ActorConfig`, `run()` |
| `run.py` | CLI entry point for single-agent RL training | Parses flags, calls `run_lib.run()` |
| `train_two_lib.py` | Two-agent mutual-play RL training: both agents learn simultaneously | Similar to `run_lib` but coordinates two learners |
| `train_two.py` | CLI entry point for two-agent RL training | Parses flags, calls `train_two_lib` |

---

### slippi_db/ - Database & Parsing Library

| File | Purpose | Key Exports |
|---|---|---|
| `parse_local.py` | Orchestrates the full replay → parquet pipeline across directories. Handles .zip/.7z archives and incremental processing | `run_parsing()`, SQLite DB operations |
| `parse_peppi.py` | Fast replay parsing via peppi-py (Rust bindings). Preferred parser | `read_slippi()`, `from_peppi()` |
| `parse_libmelee.py` | Replay parsing via libmelee. Slower alternative to peppi | `get_slp()`, `get_controller()`, `get_base_player()` |
| `preprocessing.py` | High-level parsing orchestration, cross-parser validation | `PlayerMeta`, `Metadata`, `assert_same_parse()` |
| `parsing_utils.py` | Arrow-to-parquet serialization with configurable compression | `convert_game()`, `CompressionType` |
| `upgrade_slp.py` | Upgrades legacy .slp file formats via Dolphin re-recording | `DolphinConfig` |
| `utils.py` | File I/O helpers: archive extraction (.zip/.7z), subprocess management, timing | `Timer`, concurrent.futures helpers |

---

### LAUNCHER/ - Backend

| File | Purpose | Key Exports |
|---|---|---|
| `api/__main__.py` | Uvicorn entry — binds `127.0.0.1:8000` by default | - |
| `api/app.py` | FastAPI app assembly, startup auto-detection, CORS | `app`, `lifespan` |
| `api/routes/` | REST endpoints: `play`, `agents`, `training`, `replays`, `config`, `dataset`, `bot`, `matches`, `tournaments`, `resources` | - |
| `api/training.py` | Training/eval command builder + `ProcessManager` (thread-safe subprocess singleton) | `ProcessManager`, `build_*_command` |
| `api/training_presets.py` | Built-in + user training presets (IL/RL) served to the frontend | `load_presets`, `save_preset` |
| `config.py` | Persistent INI-based configuration, environment variable overrides, path auto-detection for Slippi ISO, Dolphin, agents | `AppConfig`, model metadata caching, character/stage lists |

Frontend lives in [../slippi-ai-gui](../slippi-ai-gui) (Tauri/Svelte) and talks to this backend over HTTP.

---

### tests/ - Test Modules

| File | Purpose |
|---|---|
| `unit_tests.py` | Tests `dynamic_rnn()` vs static unroll, random spec generation |
| `networks_test.py` | Verifies unroll-vs-step consistency across network types (LSTM, GRU, MLP) |
| `observations_test.py` | Checks filter time-batched vs sequential equivalence using toy dataset |
| `saving_test.py` | Validates demo checkpoint loading |
| `rl_lib_test.py` | Tests `discounted_returns()` backward scan correctness |
| `dataset_creation_test.py` | End-to-end test of `parse_local.run_parsing()` with test replay data |
| `test_agent_outputs.py` | Agent inference output validation |

---

## Key Dependency Chain

Understanding what depends on what:

```
types.py              ← imported everywhere, the foundation
  └→ data.py          ← depends on types, paths; feeds all trainers
      └→ embed.py     ← depends on types; featurizes state for networks
          └→ networks.py     ← depends on embed; RNN cores
              └→ policies.py ← depends on networks, controller_heads, embed
                  └→ learner.py      ← IL training
                  └→ q_learner.py    ← Q-learning
                  └→ rl/learner.py   ← PPO training

envs.py ↔ dolphin.py ↔ controller_lib.py   ← environment layer
  └→ evaluators.py    ← trajectory collection
      └→ rl/run_lib.py, rl/train_two_lib.py ← RL training loops

slippi_db/            ← standalone; produces parquet consumed by data.py
LAUNCHER/             ← FastAPI backend; launches scripts that use the above
```
