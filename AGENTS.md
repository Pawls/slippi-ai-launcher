# AGENTS.md — slippi-ai-launcher

Primer for AI assistants working in this repo. Pair with [CLAUDE.md](CLAUDE.md) (ML/training focus) and [docs/per_style_rl.md](docs/per_style_rl.md).

## What this repo is

A fork of [vladfi1/slippi-ai](https://github.com/vladfi1/slippi-ai) with a full GUI launcher, Rust-accelerated reward computation, and — the focus of recent work — a **Discord-bot-driven bot match lifecycle**. Local Slippi netplay matches can be initiated remotely by a Discord bot, watched to completion, and have their outcomes taunted back into the originating Discord channel.

The separate SvelteKit/Tauri GUI ([../slippi-ai-gui](../slippi-ai-gui)) talks to this launcher's FastAPI backend. The Discord bot ([../caught-slippin](../caught-slippin)) talks to the same backend via the `/bot/*` API.

## Sibling repos & their roles

| Repo | Branch | Role |
|---|---|---|
| **slippi-ai-launcher** (this) | `discord-bot` | FastAPI backend + legacy tkinter GUI + training/netplay scripts |
| [../slippi-ai-gui](../slippi-ai-gui) | `discord-bot` | SvelteKit/Tauri 2 desktop GUI talking to the launcher |
| [../caught-slippin](../caught-slippin) | `master` | Discord bot bridging Discord slash commands → launcher API |
| `f:/melee/slippi-ai` (upstream) | — | **READ-ONLY reference.** Do not edit. Canonical slippi-ai source for cross-checking libmelee/eval_lib behavior |

## Architecture

### Directory layout
- `LAUNCHER/` — launcher code + runtime JSONs (mixed but historical)
  - `api/` — FastAPI (`__main__.py` entry, routes under `api/routes/`)
  - `screens/` — legacy tkinter screens
  - `*.json` — runtime config/state (most gitignored; see Config files below)
- `scripts/` — training, netplay, evaluation entry points
  - **`scripts/netplay.py`** — the subprocess spawned for every bot match. Emits sentinels the launcher parses.
- `slippi_ai/` — upstream ML code (leave alone unless retraining)
- `slippi_native/` — Rust reward crate (`voluntary_death_forward_fill`)
- `docs/` — [bot-api.md](docs/bot-api.md) (external Discord bot API contract), [per_style_rl.md](docs/per_style_rl.md) (the RL name-override saga)

### FastAPI entry
`python -m LAUNCHER.api` binds `127.0.0.1:8000` by default. Override with `SLIPPI_API_HOST=0.0.0.0` env or `--host` arg. Route modules:
- `api/routes/play.py` — local/netplay match launch from the Play page
- `api/routes/bot.py` — the Discord bot surface (`/bot/launch`, `/bot/status`, `/bot/roster`, `/bot/challenge/*`, `/bot/integration/*`, `/bot/end-match`)
- `api/routes/agents.py`, `config.py`, `dataset.py`, `matches.py`, `replays.py`, `resources.py`, `tournaments.py`, `training.py`

## Bot match lifecycle — critical reading

The Discord bot calls `POST /bot/launch` with `{challenger_discord_id, connect_code, character, style_name, channel_id}`. Flow:

1. **Allowlist/roster check** in `api/routes/bot.py` `launch()`.
2. **`_launch_for`** resolves the Dolphin exe and agent path, then calls `launch_netplay_session` (in `LAUNCHER/netplay_launcher.py`) which spawns `scripts/netplay.py` as a subprocess via `process_manager` (`api/training.py`).
3. **`_start_match_watchdog`** runs a daemon thread watching `info.log_lines` for `[MATCH_STARTED]`. If it doesn't appear within `challenge_timeout_sec` (default 180s) the subprocess is killed; reason tags as `timed_out`.
4. **`scripts/netplay.py`** runs its main loop. The key sentinels it prints:
   - `[MATCH_STARTED]` — emitted on the **first IN_GAME gamestate** (not on first `dolphin.step()` — that returns menu frames during matchmaking and would disarm the watchdog prematurely). Once this fires, the no-connect watchdog disarms.
   - `[GAME_RESULT] winner={ai|human|draw} ended={clean|disconnect} ai_stocks=... human_stocks=... ...` — emitted when the menu transitions out of IN_GAME. Loop breaks immediately after.
5. **Subprocess completion callback** (`_on_exit` inside `_start_match_watchdog`):
   - Parses the last `[GAME_RESULT]` line via `_extract_game_result` (returns `(winner, ended)`).
   - Decides `reason`:
     - `override=timed_out` → `timed_out` (watchdog killed it for no-connect)
     - `ended=disconnect` → `disconnected` (opponent bailed mid-match, even if the subprocess then exited cleanly)
     - `started + status=completed` → `completed`
     - else → `disconnected`
   - Calls `bs_store.clear_match(reason=...)` then `_fire_taunt(reason, challenger_id, challenger_tag, channel_id, winner)`.
6. **`_fire_taunt`** POSTs to the configured `taunt_webhook_url` in a daemon thread with Bearer `taunt_webhook_secret`. The payload is `{reason, winner, challenger_id, challenger_tag, channel_id}`. Discord bot updates its per-user record and optionally posts a heckle.

### End-match sentinel (Play-page Rage Quit button)
The GUI has "End Match (LRA+Start)" buttons (on the Play page and `BotPresenceToggle`). Clicking posts to `/play/end-match` or `/bot/end-match`, which writes a sentinel file at `<replays_dir>/.end_match` (fallback to tempdir) — the netplay subprocess polls for it at 1 Hz and holds the **L+R+A+Start** combo. **Start is staggered by 15 frames** (after L+R+A are already down) because Melee otherwise claims Start as "pause" before the reset combo completes. Subprocess then exits cleanly, which triggers the normal completion path.

### Dolphin exe selection
`_resolve_dolphin_path(cfg, use_bot_vs_human, headless)` in `api/routes/play.py`:

| `use_bot_vs_human` | `headless` | Path used | Settings key |
|---|---|---|---|
| ✅ | ✅ | **BvH headless** (e.g. `dolphin-emu-nogui.exe`) | `bot_vs_human_headless_exe` |
| ✅ | ❌ | BvH windowed | `bot_vs_human_exe` |
| ❌ | ✅ | headless (WSL/training only, NOT for netplay) | `dolphin_headless` |
| ❌ | ❌ | Slippi Netplay Dolphin folder | `dolphin_dir` |

When `use_bot_vs_human + headless` is requested but `bot_vs_human_headless_exe` is blank, `_resolve_dolphin_path` returns `None` and callers surface a specific error ("Set Settings → Bot vs Human Dolphin (headless), or un-check headless"). `_plan_headless` also probes via `dolphin_supports_headless_platform` on Windows so a misconfigured exe bombs out with a clear message instead of Dolphin crashing on `qt.qpa.plugin "headless"`.

## Config files in `LAUNCHER/`

| File | Tracked? | Purpose |
|---|---|---|
| `bot_models.example.json` | ✅ | Committed starter roster (approved character/style combos + defaults). First-run fallback. |
| `bot_models.json` | ❌ | Per-operator roster. Copy of example until the user saves through the GUI. Now gitignored. |
| `bot_allowlist.json` | ❌ | `api_token`, `allowed_discord_ids`, `allow_any_challenger`, `taunt_webhook_url`, `taunt_webhook_secret`. Auto-generated on first load (self-heals missing webhook secret). |
| `bot_local.json` | ❌ | Per-machine test overrides: `force_lan_ip`, `force_port`, `force_online_delay`. Overlays `bot_models.json` defaults at load time. Deleted when empty. |
| `bot_state.json` | ❌ | Ephemeral presence/match state. |
| `agent_library.json` | ✅ (TODO?) | Cache of pkl metadata — currently committed but arguably should also be gitignored + regenerable. |
| `match_history.json` | ❌ | Per-match history for the Play page. |
| `slippi_gui_config.ini` | ❌ | Path settings (ISO, Dolphin, agents dir, etc.). |

`bot_state.py`'s `load_models_config` merges `bot_local.json` over the example/real file and returns a unified defaults dict. `save_models_config` splits them back apart via `_LOCAL_OVERRIDE_KEYS = ("force_lan_ip", "force_port", "force_online_delay")`. GUI doesn't need to know about the split.

## Recent work (in reverse order)

Branch: `discord-bot` (all recent work). These commits each carry most of their context in the message — use `git log --grep=...` to find them:

- `80eaf26` — gitignore bot_models.json, add bot_models.example.json starter
- `240209a` — bot_local.json split (per-machine overrides live gitignored)
- `1bb8f69` — match lifecycle fixes (watchdog, loop break, disconnect detection)
- `59f999d`, `42e8dd3` — IL model (`4ccb9f2556cf`) replaces RL model for style roster; RL kept under `Master Player` for "grandmaster" tier
- `229ce60` — LRA+Start Start-stagger
- `4642840` — subprocess log timestamps + BvH headless path split + LAN test mode
- `2cfe06f` — [GAME_RESULT] sentinel in netplay.py
- `dfee9b7` — taunt webhook on match end
- `6cbe31f` — LRA+Start end-match sentinel
- `e1e7e22` — no-connect/disconnect watchdog (feature B)

## Known gotchas

- **slippi-ai at `f:/melee/slippi-ai` is READ-ONLY.** Don't edit it. Read for reference only.
- **`gm-v2.pkl` (agent_id `81b00d1edead`) is RL-finetuned with name="Master Player".** `eval_lib.build_agent` hard-overrides any `--agent.name` to `Master Player` at load time — so the IL-trained distinct pro styles on that pkl are inert. `top12_d21_imitation_3x768_v5.pkl` (`4ccb9f2556cf`) is the IL-only fallback where styles actually differentiate. See [docs/per_style_rl.md](docs/per_style_rl.md).
- **`dolphin_headless` (Settings) is WSL/training only.** Netplay matches must use BvH (`bot_vs_human_exe` or `bot_vs_human_headless_exe`) for gecko codes.
- **Bot concurrency cap is 1 today.** `/bot/launch` 409s `{"reason": "busy"}` if a match is active. There is an approved (not-yet-implemented) plan for `max_concurrent_matches` at `C:\Users\Paul\.claude\plans\does-the-polling-for-wobbly-book.md` — pick it up if the user asks for concurrent tests.
- **LAN test mode** (`force_lan_ip` + `force_port` in bot_local.json) bypasses Slippi matchmaking. Remote Discord challengers cannot connect while these are set. Clear them for production.
- **Subprocess log capture** adds `[HH:MM:SS]` timestamp prefixes via `ProcessInfo.append_log` in `api/training.py`. Don't parse log lines positionally.
- **The Discord bot's `/bot/launch` must carry `channel_id`** — the launcher round-trips it to the taunt webhook. Without it, taunts silently no-op.
- **`interaction.followup.send`** is the right API for any post-defer Discord reply. `channel.send` requires `View Channel` + `Send Messages` in the target channel; the interaction webhook doesn't.
- **Boolean flags in absl/fancyflags** need `--flag=False`, not `--noflag`.

## User profile

- Windows 11 machine, RTX 3090, runs launcher on Windows.
- Melee player, handles most testing interactively.
- Running bot matches with the BvH build at `C:\MELEE\dolphin-bvh-newest\` — `Slippi_Dolphin.exe` (windowed) and `dolphin-emu-nogui.exe` (headless).
- Discord bot handle: `PAIL#566` (hand-added by Slippi dev; Slippi doesn't support bot accounts officially yet).
- Prefers tight commits, small diffs, honest scope. Corrects freely if I misread something.

## Dev workflow

- Backend: `python -m LAUNCHER.api` from the repo root after `pip install -e .`.
- GUI: in the sibling repo; see [../slippi-ai-gui/AGENTS.md](../slippi-ai-gui/AGENTS.md).
- Discord bot: see [../caught-slippin/AGENTS.md](../caught-slippin/AGENTS.md).
- Hot-reload: backend doesn't auto-reload; restart after code changes. `scripts/netplay.py` IS read per-spawn, so netplay.py edits take effect on the next `/bot/launch` without backend restart.
- Smoke tests: `python -c "from LAUNCHER.api.routes import bot; print('OK')"` after any bot.py edit.
- Frontend typecheck: `npx svelte-check --tsconfig ./tsconfig.json` in the gui repo.

## When in doubt

- Check `docs/per_style_rl.md` for anything about agent-name overrides / RL vs IL.
- Check `C:\Users\Paul\.claude\plans\does-the-polling-for-wobbly-book.md` for the approved concurrent-matches plan (not yet executed).
- Grep for `TODO`, `FIXME`, or sentinels (`[MATCH_STARTED]`, `[GAME_RESULT]`, `[bot-taunt]`, `[bot-watchdog]`) to trace match-lifecycle flow.
