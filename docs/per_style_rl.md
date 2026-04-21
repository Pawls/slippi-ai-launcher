# Per-style behavior for Slippi-AI bot matches

## Why this matters

Bot matches launched through `/bot/launch` are conditioned on an **agent name**
(e.g. `M2K`, `Hax`, `Siddward`). The slippi-ai model has a learned embedding
per training name, and passing different names at inference should yield
different play styles.

That works for **IL-only** (imitation-learning) checkpoints. It does *not*
work on **RL-finetuned** checkpoints unless the fine-tune was per-name.

## How the RL override happens

`eval_lib.build_agent` ([`eval_lib.py:556-579`](https://github.com/vladfi1/slippi-ai/blob/main/slippi_ai/eval_lib.py)) checks the RL config of the
checkpoint. If the fine-tune ran with a single name (or a small list), it
treats that name as the ground truth and **silently overrides any
`--agent.name` you pass at inference**. The log line to grep for is:

```
WARNING  Agent trained with name(s) "['Master Player', ...]", got "Hax"
INFO     Setting agent name to "Master Player" from RL
```

At that point the policy conditions on `Master Player` for the whole match,
no matter which style the bot forwarded.

## Today: use the IL checkpoint for per-style play

`top12_d21_imitation_3x768_v5.pkl` (agent_id `4ccb9f2556cf` in
`LAUNCHER/agent_library.json`) is IL-only. Its `name_map` carries actual
distinct embeddings for dozens of pros (Hax, M2K, Cody, Amsa, Solobattle,
Ginger, Frenzy, Gosu, Aklo, Zain, Kodorin, Siddward, etc.).

To swap `bot_models.json` to it, replace the `agent_id` on each approved
entry with `4ccb9f2556cf` and verify the style_name is in that model's
`names` list. Caveat: the IL checkpoint is weaker on average than the
RL-finetuned `gm-v2.pkl` \u2014 you trade peak skill for genuine style variation.
You can also keep both in the roster: one entry per character named
"Master Player" backed by the RL checkpoint (for the "I just want to lose
hard" user) alongside per-style entries backed by the IL checkpoint.

## Later: per-style RL

If you want both the skill of RL *and* distinct styles, you run separate RL
fine-tunes, one per name, each starting from the IL checkpoint.

**Prerequisites:**
- GPU with enough VRAM to hold one training instance (gm-v2 was trained on
  a single GPU).
- `f:\melee\slippi-ai` repo, `scripts/rl/train.py` (or whatever RL entry
  point the repo exposes at the time \u2014 the file name has drifted between
  mainline and the vladfi1 forks). Read-only reference, don't edit.

**Process per name:**
1. Copy `top12_d21_imitation_3x768_v5.pkl` (or whichever IL base you want)
   into an experiments directory.
2. Start an RL run from that checkpoint with the `agent.name` flag set to
   the single name you want to condition on \u2014 e.g. `agent.name=M2K`.
   The RL script stores that name in the output checkpoint's config,
   which is what `get_name_from_rl_state` reads later.
3. Let it run for however long the original gm-v2 training did (order of
   days, depending on hardware). Checkpoint often enough to resume if
   interrupted.
4. When it plateaus, copy the final `.pkl` to
   `LAUNCHER/agents/<name>_rl.pkl` and re-scan the agent library so the
   new agent_id appears.
5. Update `bot_models.json` to map the (character, style_name) entry for
   that name at the new agent_id.

**Gotchas:**
- If `BANNED_NAMES` in `slippi-ai/slippi_ai/nametags.py` contains the
  player you want to train on, their replays were stripped from the
  dataset \u2014 the IL base won't have an embedding for them and RL from
  scratch won't bootstrap well. Unban, rebuild the dataset, retrain IL,
  *then* RL.
- The per-name RL loop overfits fast because the conditioning is trivial
  (one name). Early stopping matters. Keep the RL run short; the point is
  reward shaping on top of the IL behavior, not re-learning everything.
- Each per-style checkpoint is the size of the full model. With ~60 names
  you're looking at ~60 \u00d7 ~400MB on disk. Plan storage accordingly \u2014
  most users will only want a handful of popular names.

## Discord-bot UX notes

`/roster` already lists available (character, style_name) combos driven by
`bot_models.json`, and `/challenge` validates the combo and replies with
the roster when the combo is unknown. You don't need a new "pick from a
list" flow \u2014 the existing mechanism is sufficient so long as
`bot_models.json` truthfully reflects which styles differentiate:

- While you're on the RL checkpoint, either trim `bot_models.json` to a
  single entry per character (e.g. `style_name: "Master Player"`) or
  accept that the listed styles are labels only.
- Once you swap to the IL checkpoint (or ship per-style RL), each
  `bot_models.json` row corresponds to a real distinct policy and the
  current UX is honest.
