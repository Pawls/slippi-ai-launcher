"""Help text for training and evaluation configuration flags.

Each entry maps a dot-separated flag path to a dict with:
  explanation: Plain-English description for users new to ML.
  link:        Optional URL for further reading.

Entries are looked up by exact dot-path first, then by progressively
stripping leading segments (e.g. "p1.ai.path" → "ai.path" → "path"),
so shared entries like "ai.path" cover p1, p2, player, and opponent.
"""

# ── Imitation Learning (train.py) and shared flags ───────────────────────────

HELP = {

    # ── Runtime ──────────────────────────────────────────────────────────

    "runtime.max_runtime": dict(
        explanation=(
            "How long to train before automatically stopping, in seconds. "
            "Training longer generally produces a better model, but with "
            "diminishing returns. A good starting point is 1-4 hours "
            "(3600-14400). You can always stop early and resume later by "
            "restoring from a checkpoint."),
        link="https://developers.google.com/machine-learning/glossary#epoch",
    ),
    "runtime.log_interval": dict(
        explanation=(
            "How often (in seconds) to print training metrics to the console. "
            "Lower values give more visibility but add slight overhead. "
            "10 seconds is a good default."),
    ),
    "runtime.save_interval": dict(
        explanation=(
            "How often (in seconds) to save a checkpoint of the model to disk. "
            "Checkpoints let you resume training if it's interrupted and serve "
            "as snapshots you can evaluate. Every 5-10 minutes (300-600) is "
            "typical. More frequent saves are safer but use more disk space."),
    ),
    "runtime.num_evals_per_epoch": dict(
        explanation=(
            "How many evaluation passes to run per training epoch. Evaluation "
            "measures how well the model performs on held-out data it hasn't "
            "trained on, which helps you detect overfitting (memorizing the "
            "training data instead of learning general patterns)."),
        link="https://developers.google.com/machine-learning/crash-course/overfitting",
    ),
    "runtime.eval_at_start": dict(
        explanation=(
            "Whether to run an evaluation pass before any training begins. "
            "Useful for establishing a baseline to compare against, especially "
            "when restoring from a checkpoint."),
    ),

    # ── Dataset ──────────────────────────────────────────────────────────

    "dataset.data_dir": dict(
        explanation=(
            "Path to the directory containing your parsed replay data "
            "(parquet files). This is the output of the Dataset Management "
            "pipeline's 'Parse Replays' step. The model learns by studying "
            "the actions taken by players in these replays."),
    ),
    "dataset.meta_path": dict(
        explanation=(
            "Path to the metadata database (parsed.sqlite) created during "
            "dataset preparation. Contains information about which replays "
            "to include and their quality metrics."),
    ),
    "dataset.test_ratio": dict(
        explanation=(
            "Fraction of data reserved for testing (0.0 to 1.0). Test data "
            "is never used for training -- it's only used to evaluate how "
            "well the model generalizes. 0.1 (10%) is standard. Too low "
            "and your evaluation is unreliable; too high and you waste "
            "training data."),
        link="https://developers.google.com/machine-learning/crash-course/training-and-test-sets",
    ),
    "dataset.allowed_characters": dict(
        explanation=(
            "Which Melee characters to include in training data. Use 'all' "
            "for a multi-character model, or specify characters like "
            "'FOX,MARTH,FALCO' to train a specialist. Training on fewer "
            "characters generally produces a stronger agent for those "
            "characters, but it won't know how to play others."),
    ),
    "dataset.allowed_opponents": dict(
        explanation=(
            "Which opponent characters to include in the training data. "
            "Use 'all' to learn matchups against every character, or "
            "restrict to specific opponents. This doesn't affect which "
            "character the bot plays, only which matchup situations it "
            "learns from."),
    ),
    "dataset.allowed_names": dict(
        explanation=(
            "Filter training data to only include replays from specific "
            "player tags. Use 'all' to train on everyone, or list specific "
            "names to clone a particular player's style. Training on top "
            "players' replays produces a stronger bot."),
    ),
    "dataset.banned_names": dict(
        explanation=(
            "Player tags to exclude from training data. Use 'none' to "
            "include everyone. Useful for removing known low-quality or "
            "degenerate play patterns from your dataset."),
    ),
    "dataset.swap": dict(
        explanation=(
            "When enabled, each replay is used twice: once from player 1's "
            "perspective and once from player 2's. This effectively doubles "
            "your training data and ensures the model learns from both sides "
            "of each interaction."),
    ),
    "dataset.mirror": dict(
        explanation=(
            "When enabled, replays are horizontally mirrored (left becomes "
            "right). This doubles the effective dataset and helps the model "
            "learn symmetric play. Most useful on symmetric stages."),
    ),
    "dataset.seed": dict(
        explanation=(
            "Random seed for dataset shuffling. Using the same seed ensures "
            "reproducible train/test splits. Change the seed to get a "
            "different random split of the data."),
    ),

    # ── Data ─────────────────────────────────────────────────────────────

    "data.batch_size": dict(
        explanation=(
            "Number of replay sequences processed simultaneously in each "
            "training step. Larger batches give more stable gradient "
            "estimates but use more GPU memory. "
            "32 is a good default for an RTX 3090. If you get out-of-memory "
            "errors, reduce this. If training seems noisy, try increasing it."),
        link="https://developers.google.com/machine-learning/glossary#batch-size",
    ),
    "data.unroll_length": dict(
        explanation=(
            "Number of consecutive game frames in each training sequence "
            "used for backpropagation through time. For recurrent networks "
            "(LSTM/GRU), longer unrolls let gradients flow over more frames, "
            "helping the network learn longer temporal dependencies. "
            "This does NOT limit the model's planning horizon during play — "
            "that is determined by the discount factor (reward_halflife). "
            "64 frames is a good balance between memory usage and gradient "
            "quality. Increasing to 128 uses ~2x memory for marginal gains."),
    ),
    "data.damage_ratio": dict(
        explanation=(
            "Scaling factor for the damage component of the reward signal. "
            "The reward combines damage dealt and stocks taken to guide "
            "the value function. A small value like 0.01 keeps damage as "
            "a secondary signal relative to stocks."),
    ),
    "data.compressed": dict(
        explanation=(
            "Whether the replay data is stored in compressed format. "
            "Should match how you processed the data in the Dataset "
            "Management pipeline. Compressed data uses less disk space "
            "but requires CPU time to decompress during training."),
    ),
    "data.num_workers": dict(
        explanation=(
            "Number of background processes for loading and preparing data. "
            "More workers keep the GPU fed with data, preventing it from "
            "sitting idle. 0 means data loading happens in the main process. "
            "2-4 is usually enough; more helps if you have slow storage."),
    ),

    # ── Learner ──────────────────────────────────────────────────────────

    "learner.learning_rate": dict(
        explanation=(
            "Controls how much the model adjusts its weights on each "
            "training step. Think of it as the 'step size' when climbing "
            "down a hill toward better performance. "
            "Too high (>1e-3) and training becomes unstable and may diverge. "
            "Too low (<1e-5) and training is very slow. "
            "1e-4 (0.0001) is a safe starting point for most configurations."),
        link="https://developers.google.com/machine-learning/crash-course/reducing-loss/learning-rate",
    ),
    "learner.compile": dict(
        explanation=(
            "Whether to compile the training step into an optimized graph. "
            "Compilation makes training significantly faster after an initial "
            "warmup period. Should almost always be True."),
    ),
    "learner.jit_compile": dict(
        explanation=(
            "Whether to use XLA just-in-time compilation for the training "
            "step. JIT compilation can provide additional speed improvements "
            "on top of regular compilation by fusing operations. May cause "
            "issues on some hardware. Try True for speed; revert to False "
            "if you encounter errors."),
        link="https://www.tensorflow.org/xla",
    ),
    "learner.decay_rate": dict(
        explanation=(
            "Rate at which the learning rate decreases over time. A value "
            "of 0 means constant learning rate. Values like 0.95-0.99 "
            "gradually reduce the learning rate, which can help fine-tune "
            "the model in later stages of training. Most users should "
            "start with 0 (no decay)."),
        link="https://developers.google.com/machine-learning/glossary#learning-rate-decay",
    ),
    "learner.value_cost": dict(
        explanation=(
            "Weight of the value function loss relative to the policy loss. "
            "The value function estimates how 'good' a game state is "
            "(like evaluating a chess position). Training it alongside the "
            "policy helps the model understand which states lead to winning. "
            "0.5 means the value loss is weighted at half the policy loss."),
        link="https://spinningup.openai.com/en/latest/spinningup/rl_intro.html#value-functions",
    ),
    "learner.reward_halflife": dict(
        explanation=(
            "How quickly future rewards are discounted, in seconds. A "
            "shorter halflife (1-2s) makes the model focus on immediate "
            "actions (hitting the opponent now). A longer halflife (4-8s) "
            "makes it plan further ahead (setting up combos, edge-guarding). "
            "4 seconds is a good balance for Melee."),
        link="https://developers.google.com/machine-learning/glossary#discount-factor",
    ),

    # ── Policy ───────────────────────────────────────────────────────────

    "policy.train_value_head": dict(
        explanation=(
            "Whether to train a value head alongside the policy. The value "
            "head predicts the expected outcome (win/loss) from any game "
            "state. This is essential for reinforcement learning later, "
            "and also helps imitation learning by providing a richer "
            "training signal. Keep this True unless you have a reason not to."),
    ),
    "policy.delay": dict(
        explanation=(
            "Number of frames of input delay the model is trained with. "
            "This must match the delay used during actual gameplay. In "
            "Slippi online play, there are typically 2+ frames of delay. "
            "Training with delay teaches the model to anticipate and "
            "act earlier, compensating for the lag. Higher delay requires "
            "the model to predict further into the future."),
    ),

    # ── Network ──────────────────────────────────────────────────────────

    "network.name": dict(
        explanation=(
            "The neural network architecture to use. This determines how "
            "the model processes game state information. 'mlp' (Multi-Layer "
            "Perceptron) is simpler and faster; 'lstm' and 'gru' are "
            "recurrent networks that can remember past frames, which helps "
            "with timing-dependent actions like combos and DI."),
        link="https://colah.github.io/posts/2015-08-Understanding-LSTMs/",
    ),

    # ── Embed ────────────────────────────────────────────────────────────

    "embed.with_randall": dict(
        explanation=(
            "Whether to include Randall the Cloud's position as an input "
            "to the model. Randall is the moving platform on Yoshi's Story "
            "that can save players from dying. Including it helps the model "
            "learn to use (and play around) Randall's position."),
    ),
    "embed.with_fod": dict(
        explanation=(
            "Whether to include Fountain of Dreams' platform positions. "
            "FoD has moving platforms whose height changes during gameplay. "
            "Including this information helps the model adapt to the "
            "current platform layout."),
    ),

    # ── Top-level ────────────────────────────────────────────────────────

    "max_names": dict(
        explanation=(
            "Maximum number of distinct player name tags the model can "
            "recognize and condition on. When the model knows which player "
            "it's imitating, it can reproduce their specific style. 16 is "
            "usually more than enough."),
    ),
    "expt_root": dict(
        explanation=(
            "Root directory where training experiments are saved. Each "
            "training run creates a subdirectory here containing "
            "checkpoints, logs, and configuration. You can organize "
            "different experiments by changing this."),
    ),
    "expt_dir": dict(
        explanation=(
            "Optional subfolder name for this experiment within expt_root. "
            "If left empty, a name is generated automatically from the tag "
            "and timestamp (e.g. 'experiments/2024-01-15_fox_specialist'). "
            "Set this to resume a specific experiment or to use a custom "
            "name. This is NOT a full path — just the folder name under "
            "expt_root."),
    ),
    "tag": dict(
        explanation=(
            "Optional tag appended to the experiment directory name. "
            "Useful for adding a brief description, like 'fox-specialist' "
            "or 'high-lr-test', to help you find experiments later."),
    ),
    "restore_pickle": dict(
        explanation=(
            "Path to a previously saved model checkpoint (.pkl file) to "
            "resume training from. This lets you continue training where "
            "you left off, or fine-tune an existing model on different "
            "data. Leave empty to start from scratch."),
    ),
    "is_test": dict(
        explanation=(
            "Whether this is a test run. When True, the experiment uses "
            "a temporary directory and doesn't save permanent checkpoints. "
            "Useful for verifying your configuration works before "
            "committing to a long training run."),
    ),
    "version": dict(
        explanation=(
            "Model version number. This must match the saving format. "
            "Generally you should not change this unless migrating "
            "between different versions of the codebase."),
    ),

    # ── Q-Learning specific ──────────────────────────────────────────────

    "learner.train_sample_policy": dict(
        explanation=(
            "Whether to train the policy network using samples from the "
            "Q-function. This bridges Q-learning and policy learning: "
            "instead of just learning Q-values, the model also learns "
            "a policy that takes the best actions according to those "
            "Q-values."),
    ),
    "learner.num_samples": dict(
        explanation=(
            "Number of action samples to draw when computing Q-learning "
            "targets. More samples give a better estimate of the best "
            "action but are more expensive. 1 is the minimum; higher "
            "values (4-8) can improve training stability."),
    ),
    "learner.q_policy_imitation_weight": dict(
        explanation=(
            "Weight for imitating the expert policy during Q-learning. "
            "This combines behavioral cloning with Q-learning: the model "
            "learns both from expert demonstrations and from its own "
            "Q-value estimates. 0 means pure Q-learning; higher values "
            "anchor the policy closer to the expert."),
    ),
    "learner.q_policy_expected_return_weight": dict(
        explanation=(
            "Weight for maximizing expected return in the Q-policy loss. "
            "This encourages the policy to take actions with high Q-values "
            "(predicted long-term reward). Combined with the imitation "
            "weight, this creates a spectrum between pure imitation and "
            "pure value-based learning."),
    ),
    "initialize_policies_from": dict(
        explanation=(
            "Path to a pre-trained imitation learning model to initialize "
            "the Q-learning policy from. Starting Q-learning from an IL "
            "model is much more effective than random initialization, "
            "because the policy already knows reasonable actions."),
    ),

    # ── RL Evaluator (Q-learning) ────────────────────────────────────────

    "rl_evaluator.use": dict(
        explanation=(
            "Whether to periodically evaluate the model by running actual "
            "games in Dolphin during training. This gives you a real "
            "measure of in-game performance rather than just loss metrics. "
            "Requires a Dolphin installation and ISO."),
    ),
    "rl_evaluator.interval_seconds": dict(
        explanation=(
            "How often (in seconds) to run an in-game evaluation. More "
            "frequent evaluations give better visibility into training "
            "progress but slow down training. Every 15 minutes (900) is "
            "a reasonable default."),
    ),

    # ── Controller head ──────────────────────────────────────────────────

    "controller_head.name": dict(
        explanation=(
            "How the model represents controller outputs (buttons, sticks). "
            "'independent' treats each input separately. 'autoregressive' "
            "models dependencies between inputs (e.g., the stick position "
            "may depend on which button is pressed), which is more accurate "
            "but slower."),
    ),

    # ── Observation ──────────────────────────────────────────────────────

    "observation.animation.mask": dict(
        explanation=(
            "Whether to group rare character animations into a single "
            "'other' category. This reduces the number of animation states "
            "the model needs to learn, making training more efficient "
            "without losing important information. Should almost always "
            "be True."),
    ),

    # ── Value Function ───────────────────────────────────────────────────

    "value_function.train_separate_network": dict(
        explanation=(
            "Whether the value function uses its own neural network, "
            "separate from the policy network. A separate network prevents "
            "the value function's training from interfering with the "
            "policy's. This is generally beneficial and recommended."),
    ),
    "value_function.separate_network_config": dict(
        explanation=(
            "Whether the separate value network uses its own architecture "
            "configuration. When True, you can configure the value network "
            "independently (e.g., make it smaller for efficiency). When "
            "False, it mirrors the policy network's architecture."),
    ),

    # ── Evaluation (eval_two.py / run_evaluator.py) ─────────────────────
    #
    # These use suffix-based fallback, so "ai.path" covers p1.ai.path,
    # p2.ai.path, player.ai.path, and opponent.ai.path automatically.

    # -- Player / opponent setup --

    "type": dict(
        explanation=(
            "Who controls this port. 'ai' loads a trained agent from a "
            "checkpoint. 'human' opens a controller port for you to play "
            "(you must start Dolphin with a controller connected). 'cpu' "
            "uses the built-in Melee CPU at the chosen level."),
    ),
    "character": dict(
        explanation=(
            "Which Melee character this player uses. For AI agents this "
            "must match the character the model was trained on, otherwise "
            "the inputs will not make sense. For CPU players you can pick "
            "any character."),
    ),
    "level": dict(
        explanation=(
            "CPU difficulty level (1-9). Only applies when type is 'cpu'. "
            "Level 9 is the hardest built-in CPU. This has no effect on "
            "AI or human players."),
    ),

    # -- Agent flags (shared across p1.ai.*, p2.ai.*, player.ai.*, etc.) --

    "ai.path": dict(
        explanation=(
            "Path to the trained agent's checkpoint file (.pkl). This is "
            "the model that will play in-game. You can find checkpoints in "
            "your experiment directory (e.g. experiments/train_two/) or in "
            "the agents/ folder for pre-trained IL models."),
    ),
    "ai.name": dict(
        explanation=(
            "Player nametag the agent uses. If the model was trained on "
            "replays from multiple players, the nametag selects whose "
            "style to imitate. Use the name exactly as it appears in the "
            "training data (case-sensitive)."),
    ),
    "ai.sample_temperature": dict(
        explanation=(
            "Controls how 'random' the agent's actions are. 1.0 is the "
            "default (matches training behavior). Lower values (e.g. 0.5) "
            "make the agent more deterministic, always picking the highest-"
            "probability action. Higher values (e.g. 1.5) add more variety "
            "but may reduce quality."),
    ),
    "ai.compile": dict(
        explanation=(
            "Whether to compile the agent's inference function into an "
            "optimized graph. Compilation adds a short startup delay but "
            "makes each step faster. Should almost always be True."),
    ),
    "ai.jit_compile": dict(
        explanation=(
            "Whether to use XLA just-in-time compilation for agent "
            "inference. Can improve speed on supported hardware but may "
            "cause issues on some systems. Try True for speed; revert "
            "to False if you see errors."),
        link="https://www.tensorflow.org/xla",
    ),
    "ai.name_change_mode": dict(
        explanation=(
            "How the agent's nametag changes between games. 'FIXED' keeps "
            "the same name every game. Other modes cycle through available "
            "names, which is useful for evaluating how different player "
            "styles perform."),
    ),
    "ai.mirror": dict(
        explanation=(
            "Whether to mirror (flip left/right) the agent's inputs and "
            "outputs. Useful for testing symmetry or if the model was "
            "trained on mirrored data."),
    ),
    "ai.async_inference": dict(
        explanation=(
            "Whether to run the agent's neural network inference in a "
            "background thread. This can improve frame rate by overlapping "
            "inference with the game's frame processing, but adds one "
            "frame of latency."),
    ),
    "ai.fake": dict(
        explanation=(
            "Use a fake agent that outputs random actions instead of "
            "loading a real model. Only useful for testing that the "
            "evaluation pipeline works without needing a trained model."),
    ),
    "ai.batch_steps": dict(
        explanation=(
            "Number of consecutive frames to batch together for inference. "
            "0 means one frame at a time. Higher values can improve GPU "
            "throughput in benchmarks but add latency. Not useful for "
            "interactive play."),
    ),

    # -- Dolphin configuration --

    "dolphin.path": dict(
        explanation=(
            "Path to the folder containing the Dolphin emulator executable. "
            "This should be the Slippi-modified version of Dolphin, not "
            "the standard one. On Windows this is usually in your Slippi "
            "Launcher installation folder."),
    ),
    "dolphin.iso": dict(
        explanation=(
            "Path to your Super Smash Bros. Melee 1.02 NTSC ISO file. "
            "This is the game ROM that Dolphin loads. You need a legally "
            "obtained copy."),
    ),
    "dolphin.online_delay": dict(
        explanation=(
            "Number of frames of artificial input delay added to simulate "
            "online play conditions. Slippi Online typically uses 2 frames "
            "of delay. Set this to match the delay your model was trained "
            "with for best results. 0 means no artificial delay (local "
            "play conditions)."),
    ),
    "dolphin.stage": dict(
        explanation=(
            "Which stage to play on. RANDOM_STAGE picks a random "
            "tournament-legal stage each game. You can also choose a "
            "specific stage like BATTLEFIELD or FINAL_DESTINATION."),
    ),
    "dolphin.fullscreen": dict(
        explanation=(
            "Whether to run Dolphin in fullscreen mode. Useful when "
            "playing against the AI yourself and you want a full-screen "
            "game experience."),
    ),
    "dolphin.headless": dict(
        explanation=(
            "Run Dolphin without graphics or audio. In headless mode, "
            "games run as fast as the CPU allows, which is much faster "
            "than real-time. Used for benchmarking and data collection "
            "where you don't need to see the game."),
    ),
    "dolphin.blocking_input": dict(
        explanation=(
            "Whether the game waits for the AI to send its input before "
            "advancing to the next frame. When True, the game runs at "
            "the speed of the AI's inference. When False, the game runs "
            "at normal speed and the AI must keep up."),
    ),
    "dolphin.save_replays": dict(
        explanation=(
            "Whether to save .slp replay files for each game played. "
            "Replays can be watched later in Slippi Launcher or analyzed "
            "with replay tools."),
    ),
    "dolphin.replay_dir": dict(
        explanation=(
            "Directory to save replay files to when save_replays is "
            "enabled. If not set, replays go to the default Slippi "
            "replay directory."),
    ),
    "dolphin.render": dict(
        explanation=(
            "Whether to render video frames. Disabling rendering makes "
            "games run faster but you won't be able to see what's "
            "happening. Only works with vladfi1's custom Slippi fork."),
    ),
    "dolphin.copy_home_directory": dict(
        explanation=(
            "Whether to copy the Dolphin home/config directory to a temp "
            "location before launching. Useful when running multiple "
            "Dolphin instances in parallel so they don't conflict with "
            "each other's settings."),
    ),
    "dolphin.gfx_backend": dict(
        explanation=(
            "Which graphics backend Dolphin uses for rendering. Leave "
            "empty for the default (usually Vulkan). Options include "
            "'OGL' (OpenGL) and 'Vulkan'. Only matters when rendering "
            "is enabled."),
    ),
    "dolphin.disable_audio": dict(
        explanation=(
            "Disable Dolphin's audio output. Useful for headless runs "
            "or when running many instances. Slightly reduces CPU usage."),
    ),
    "dolphin.audio_backend": dict(
        explanation=(
            "Which audio backend Dolphin uses. Leave empty for the "
            "default. Only matters when audio is not disabled."),
    ),
    "dolphin.emulation_speed": dict(
        explanation=(
            "Game speed multiplier. 1.0 is normal speed, 0.5 is half "
            "speed, 2.0 is double speed. Set to 0 for unlimited speed "
            "(as fast as CPU allows). Only works with mainline Dolphin "
            "builds."),
    ),
    "dolphin.overclock": dict(
        explanation=(
            "CPU overclock multiplier for the emulated GameCube CPU. "
            "Higher values (e.g. 4.0 for 400%) make the emulated CPU "
            "run faster, which can speed up loading screens and menus. "
            "Set to None to disable."),
    ),
    "dolphin.infinite_time": dict(
        explanation=(
            "When enabled, the game clock never runs out and stocks "
            "are not enforced. Games continue indefinitely until "
            "manually stopped. Useful for long evaluation runs."),
    ),
    "dolphin.console_timeout": dict(
        explanation=(
            "How many seconds to wait for the Dolphin console to respond "
            "before raising an error. None means wait indefinitely. Set "
            "a value if you want evaluation to fail fast when Dolphin "
            "hangs."),
    ),
    "dolphin.slippi_port": dict(
        explanation=(
            "Local TCP port for communicating with the Dolphin process. "
            "Each Dolphin instance needs a unique port. Leave as None "
            "to auto-assign. Only set this manually if you're running "
            "multiple instances and need explicit port control."),
    ),
    "dolphin.gecko_codes_file": dict(
        explanation=(
            "Path to a file with custom Gecko codes in INI format. "
            "Gecko codes are game modifications (e.g. stage striking, "
            "training mode features). Applied when Dolphin launches."),
    ),
    "dolphin.log_level": dict(
        explanation=(
            "Dolphin log verbosity. 0 = disabled, 3 = warnings only "
            "(default), 5 = everything. Higher levels produce more "
            "output and may slow down emulation."),
    ),

    # -- Top-level eval_watch flags --

    "num_games": dict(
        explanation=(
            "Number of games to play before stopping. Leave empty (None) "
            "to play indefinitely. Each game runs until a winner is "
            "decided or the timer runs out."),
    ),
    "use_gpu": dict(
        explanation=(
            "Whether to run the agent's neural network on the GPU. "
            "GPU inference is faster and recommended if you have a "
            "compatible NVIDIA GPU. Set to False to use CPU only, "
            "which works but is slower."),
    ),

    # -- Top-level eval_benchmark flags --

    "rollout_length": dict(
        explanation=(
            "Number of game frames to simulate per evaluation rollout. "
            "3600 frames = 1 minute of gameplay at 60 FPS. The default "
            "of 7200 (2 minutes) gives a reasonable sample of "
            "performance. Longer rollouts give more reliable statistics "
            "but take proportionally longer."),
    ),
    "num_envs": dict(
        explanation=(
            "Number of parallel Dolphin environments to run. More "
            "environments collect data faster by running multiple games "
            "simultaneously, but each one uses ~40 MB of RAM and a "
            "CPU core. Start with 1 and increase based on your hardware."),
    ),
    "self_play": dict(
        explanation=(
            "Whether both players use the same agent (self-play). "
            "Useful for measuring how well an agent performs against "
            "itself, which should be roughly even if the model is "
            "well-calibrated."),
    ),
    "async_envs": dict(
        explanation=(
            "Whether to run Dolphin environments asynchronously. When "
            "True, environments step independently and the agent "
            "processes whichever environment is ready first. This "
            "improves throughput when environments have variable step "
            "times."),
    ),
    "num_env_steps": dict(
        explanation=(
            "Number of environment steps to batch together before "
            "sending to the agent. Higher values improve GPU utilization "
            "by processing more frames at once, but add latency. 0 "
            "means no batching. Only useful for benchmarking throughput."),
    ),
    "inner_batch_size": dict(
        explanation=(
            "Number of environments to run sequentially within each "
            "batch. Groups environments so they share a Dolphin process, "
            "reducing overhead. Useful when running many environments. "
            "1 means each environment runs in its own process."),
    ),
    "num_agent_steps": dict(
        explanation=(
            "Number of agent inference steps to batch together in time. "
            "Higher values amortize the overhead of each inference call "
            "but add latency. 0 means one step at a time. Only useful "
            "for benchmarking."),
    ),
    "fake_envs": dict(
        explanation=(
            "Use fake environments that return random game states instead "
            "of running Dolphin. Useful for testing the evaluation "
            "pipeline and profiling agent inference speed without "
            "needing Dolphin or an ISO."),
    ),
    "tf_profile": dict(
        explanation=(
            "Enable TensorFlow's built-in profiler. Generates a trace "
            "that can be viewed in TensorBoard to identify performance "
            "bottlenecks. Only useful for development and debugging."),
        link="https://www.tensorflow.org/guide/profiler",
    ),
}
