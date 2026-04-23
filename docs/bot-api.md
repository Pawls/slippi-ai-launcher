# Discord Bot Command API

The launcher exposes a small HTTP surface that an external Discord/LLM bot
calls to check availability, look up which models are approved, and launch a
netplay match against a Discord challenger. All endpoints live under
`/bot/*` on the launcher (default `http://127.0.0.1:8000`).

## Setup

1. Start the launcher. On first boot, `LAUNCHER/bot_allowlist.json` is
   generated with a fresh `api_token` and an empty Discord-ID allowlist.
2. Edit `bot_allowlist.json` to add the Discord user IDs that are permitted
   to challenge the bot:
   ```json
   {
     "api_token": "<keep this secret>",
     "allowed_discord_ids": ["1234567890", "0987654321"]
   }
   ```
3. Edit `LAUNCHER/bot_models.json` to list the `(character, style_name)`
   combinations the LLM may offer, each pointing at an agent-store ID:
   ```json
   {
     "approved_models": [
       { "character": "Ganon", "style_name": "Pawl",   "agent_id": "abc123de" },
       { "character": "Marth", "style_name": "TSC", "agent_id": "def456gh" }
     ],
     "defaults": { "delay": 2, "sample_temperature": 1.0, "use_bot_vs_human": true }
   }
   ```
   Find `agent_id` values in `LAUNCHER/agent_library.json` (or the GUI's
   Agents page — the ID column).

## Auth

Every endpoint (except `/bot/local-token`) requires
`Authorization: Bearer <api_token>`. `/bot/local-token` is loopback-only and
exists for the GUI to bootstrap its own token; external bots should read
the token out of `bot_allowlist.json` directly.

## Endpoints

### `GET /bot/presence`
Cheap polling target. Poll every ~15–30s and diff `state` between polls —
transition `offline → available*` is when the bot should post "SEARCHIN?!".

```json
{ "state": "available", "in_match": false, "last_changed": "2026-04-17T20:14:02+00:00" }
```

### `GET /bot/roster`
LLM-facing menu of approved models, grouped by character. Use this to
answer "can I play your Peach as Pawl?" before launching.

```json
{
  "state": "available",
  "in_match": false,
  "roster": [
    { "character": "falco", "styles": ["TSC", "Char"] },
    { "character": "fox",   "styles": ["Kalo"] }
  ]
}
```

### `POST /bot/launch`
Request a match. Body:
```json
{
  "challenger_discord_id": "1234567890",
  "challenger_tag": "PawlstotheWall",
  "connect_code": "PAWL#723",
  "character": "sheik",
  "style_name": "Master Player"
}
```

Responses:
- **Presence `available`** → `{ "status": "launching", "match_id": "..." }` (200). Dolphin is spawning.
- **Presence `available_with_approval`** → `{ "status": "pending_approval", "challenge_id": "...", "poll_url": "/bot/challenge/<id>", "expires_at": "..." }` (200). Poll `/bot/challenge/<id>` until status changes.
- **Offline** → 409 `{ "reason": "offline" }`.
- **Busy** → 409 `{ "reason": "busy" }` (active match) or `{ "reason": "pending_approval" }` (another challenge awaiting approval).
- **Queue full** → 409 `{ "reason": "queue_full" }` (approval mode, ≥3 pending).
- **Not allowlisted** → 403 `{ "reason": "not_allowed" }`.
- **Unknown model** → 404 `{ "reason": "unknown_model" }`.

### `GET /bot/challenge/{challenge_id}`
Poll for an approval-mode outcome. Shape:
```json
{ "status": "pending" | "approved" | "denied" | "expired" | "unknown",
  "match_id": "..." | null,
  "headless": true | false | null }
```
Pending TTL is 2 minutes; after that the status flips to `expired`. Resolved
records stay queryable for ~32 decisions.

### `GET /bot/status`
Full snapshot for debugging — returns presence, active match, and all
pending challenges. Same shape the GUI approval tray consumes.

### `POST /bot/presence` (GUI only)
Body: `{ "state": "offline" | "available" | "available_with_approval" }`.
Toggled from the Play page's presence widget.

### `POST /bot/approve` (GUI only)
Body: `{ "challenge_id": "...", "decision": "approve" | "deny", "headless": true }`.
Approving with `headless: false` spawns a visible Dolphin so Paul can
watch the bot play.

### `GET /bot/local-token`
Loopback-only convenience for the GUI to fetch the bearer token without
requiring the user to paste it. External callers should use
`bot_allowlist.json` directly.

## Example conversation flow

1. User: *"Yo smash bot, I bet I can beat your Sheik playing like \<insert approved player name\>. My code is PAWL#723"*
2. LLM calls `GET /bot/roster` → confirms `sheik` has `Dash` in `styles`.
3. LLM calls `POST /bot/launch` with challenger details.
4. If response is `launching`, LLM replies in Discord: *"Get ready."*
5. If response is `pending_approval`, LLM replies: *"Hold tight — asking Paul."* and polls `GET /bot/challenge/<id>` every few seconds until approved/denied/expired.

## Concurrency contract

Exactly one active match at a time. A second challenge while a match is
running returns 409 `busy` — the LLM should trash-talk and tell the user
to try again later.

---

# Live match events (NEW)

The launcher now emits **mid-match behavioral events** the bot can react
to in real time — e.g. "that's your 4th shield-grab in 30 seconds, stop
stalling." Previously the only signal you got during a match was the
post-match taunt webhook (still available, unchanged). Live events run
alongside that stream.

## Opt-in — this is off by default

The feature is gated behind a master toggle in `bot_allowlist.json`:

```json
{
  "live_events": {
    "enabled": false,
    "max_per_match": 5,
    "types": { "shield_grab_spam": { ... }, "roll_spam": { ... }, ... }
  }
}
```

**Nothing streams until the launcher operator (the person hosting the
bot) flips `enabled: true`.** The operator does this through the Studio
GUI — Discord Integration panel → "Live Reactions" section. When off,
the launcher passes no flag to the netplay subprocess; the observer
thread never runs and frame-time cost stays at zero.

An LLM bot that consumes live events when available can simply start
polling the endpoint — if the operator hasn't enabled the feature,
the poll just returns empty. No coordination needed.

## Consuming live events — `GET /bot/live-events`

```
GET /bot/live-events?since=<cursor>&limit=<N>
Authorization: Bearer <api_token>
```

Response:
```json
{
  "events": [
    {
      "id": 42,
      "match_id": "m_abc123",
      "type": "smash_spam",
      "frame": 3840,
      "timestamp": "2026-04-22T19:11:03.412Z",
      "severity": "medium",
      "player_port": 2,
      "stats": { "move": "FSMASH", "count": 3 },
      "text_hint": "3 fsmashes in a row",
      "challenger_id": "1234567890",
      "challenger_tag": "Pawls#723",
      "channel_id": "987654321098765432"
    },
    ...
  ],
  "cursor": 42
}
```

- **Ring-buffered** (size 500) — poll at your own pace. As long as you
  don't fall more than 500 events behind, you won't miss anything.
- `since=<cursor>` excludes events with `id <= cursor`. Send the
  previous response's `cursor` back on the next poll (same pattern as
  `GET /bot/taunts`).
- `limit` caps the per-response count (default 100, max 500).
- Empty `events` array + unchanged cursor = no new events (normal
  steady state between triggers). Just poll again.
- **Suggested poll cadence**: every 2–5 seconds during a match, or
  longer when idle. The endpoint is cheap; under-polling just delays
  reactions.

## Event schema

All events share this envelope:

| Field | Type | Notes |
|---|---|---|
| `id` | int | Monotonic, never reused. Use for `since` cursor. |
| `match_id` | string | Scopes per-match cooldowns. Persists across rematches within a single bot subprocess. |
| `type` | string | The event type — see table below. |
| `frame` | int | Monotonic frame counter from the netplay subprocess. Not a wall clock. |
| `timestamp` | ISO 8601 UTC | When the launcher ingested the event. |
| `severity` | string | Always `"medium"` in MVP. Reserved for future heat-level tiering. |
| `player_port` | int | 1–4. Which port on the console triggered the event (always the opponent — the AI's own behavior is not observed). |
| `stats` | object | Type-specific extra fields — see per-type table. |
| `text_hint` | string | **Optional fallback copy** the non-LLM bot uses verbatim. LLMs should compose their own line from `type` + `stats`; `text_hint` is a safety net if your LLM pipeline is down. |
| `challenger_id` | string | Discord user ID of the challenger. Use `<@ID>` syntax to mention. |
| `challenger_tag` | string | Discord tag (e.g. `Pawls#723`). Fallback for mentions when `challenger_id` isn't numeric. |
| `channel_id` | string | Discord channel ID where the `/challenge` was issued. Post live reactions here. |

### Event types

| `type` | Meaning | `stats` fields |
|---|---|---|
| `shield_grab_spam` | Opponent triggered SHIELD→GRAB (shield-grab) `count` times in a `window_sec` window. Shield-grabbing is dishonorable because it signals over-defensive stalling play. | `count` (int, ≥ threshold), `window_sec` (float) |
| `roll_spam` | Opponent rolled (any direction, any variant) `count` times in a `window_sec` window. | `count` (int), `window_sec` (float) |
| `ledge_camp` | Opponent's cumulative ledge-hang time exceeded `dwell_sec` within the last `window_sec`. Discontinuous ledge visits add up. | `dwell_sec` (float, observed dwell), `window_sec` (float) |
| `smash_spam` | Opponent threw the same grounded smash family `count` times in a row (no other smash family between, gap < 10s). Hit/whiff doesn't matter. | `move` (string: `"FSMASH"`, `"UPSMASH"`, `"DOWNSMASH"`), `count` (int, ≥ 3) |

**FSMASH groups all angle variants** (forward-smash angled up, down, straight — they're the same move choice).

New event types may be added over time. **Your consumer should switch
on `type` and silently ignore unknown types** rather than crashing — the
launcher is allowed to emit new types without a coordinated rollout.

## Rate expectations

Per-match cap (default 5) and per-(match, type) cooldown (default 30s
per type) are enforced **server-side in the launcher** — you will not
see floods, even if a pathological detector run would otherwise emit
many. By the time an event reaches you, it has already passed:

1. Per-detector debounce inside the netplay subprocess
2. Master toggle + per-type enable check
3. Per-`(match_id, type)` cooldown
4. Per-match cap

Expected volume: **2–5 events per match**, spaced roughly 30s apart or
longer. An idle / normally-playing match produces zero events.

## Match context

Events arrive while a match is active. The `match_id` field correlates
events to the `/bot/launch` response you got at match start and to
subsequent `/bot/taunts` match-end events. Use it to scope any per-match
state you keep (history, streaks, composed-line dedup, etc.).

Between-match rematches within the same bot subprocess **share the same
`match_id`** (the launcher considers "one `/bot/launch` call" as one
match for bookkeeping). The per-match cap and cooldowns span the whole
subprocess lifetime — not reset on rematch. If this becomes a problem
in practice, raise it and we can add per-game resets.

## Ordering guarantees

- **Within a single match, live events are strictly ordered by `id`**.
- **Events for a match always arrive before the match-end taunt** for
  the same match (the netplay subprocess flushes the observer before
  printing `[GAME_RESULT]` and exiting).
- Cross-match ordering matches the temporal order: events from match N
  all land before any event from match N+1.

## Example consumer loop

Polling alongside the existing `/bot/taunts` loop:

```python
import time, httpx

BASE = "http://127.0.0.1:8000"  # or remote over Tailscale
AUTH = {"Authorization": f"Bearer {api_token}"}

live_cursor = 0
taunt_cursor = 0

while True:
    # Live events — post heckle immediately
    r = httpx.get(f"{BASE}/bot/live-events",
                  params={"since": live_cursor}, headers=AUTH, timeout=5)
    data = r.json()
    for ev in data["events"]:
        await post_live_heckle(ev)  # your LLM composes + sends to channel_id
    live_cursor = data["cursor"]

    # Match-end taunts — existing flow
    r = httpx.get(f"{BASE}/bot/taunts",
                  params={"since": taunt_cursor}, headers=AUTH, timeout=5)
    data = r.json()
    for ev in data["events"]:
        await post_match_end_taunt(ev)
    taunt_cursor = data["cursor"]

    time.sleep(3)
```

## Composing an LLM reply

For each event, we recommend prompting the LLM with:

- `type` (verbatim, e.g. `smash_spam`)
- `stats` (verbatim JSON)
- The challenger's display name (`<@{challenger_id}>` for a proper
  mention, falling back to `challenger_tag`)
- Any per-user history you track (W/L, streak of live events this
  match, etc.)

Keep replies short (1 sentence is usually right) and voice-appropriate
to your bot. Users expect heckling, not encyclopedia entries.

Example prompt fragment:

```
The human just triggered a live gamestate event in a Melee match.
Event type: smash_spam
Stats: {"move": "FSMASH", "count": 3}
User: <@1234567890>

Write one short, in-character Discord line heckling them about this
specific behavior. Do NOT explain the mechanic; assume the user knows
Melee. Include the mention token `<@1234567890>` exactly once.
```

## Local push alternative (not typical for external bots)

For completeness: the launcher can also **push** events to a local
aiohttp webhook instead of being polled. That's the integration
[caught-slippin](../../caught-slippin) uses — it runs on the same box
as the launcher. Over Tailscale or other tunneled setups, polling is
simpler because the bot doesn't have to host an inbound endpoint
reachable from the launcher.

Bot-side endpoint shape (when implemented):
```
POST /live-event
Authorization: Bearer <taunt_webhook_secret>
Content-Type: application/json

{ ...same schema as the polled events... }
```

The webhook URL is derived from `taunt_webhook_url` by swapping the
path — if `taunt_webhook_url = http://127.0.0.1:8787/taunt`, the
launcher POSTs live events to `http://127.0.0.1:8787/live-event` with
the same `taunt_webhook_secret`. External remote bots can ignore this
and use `GET /bot/live-events` polling instead.

## Unchanged: post-match taunts

`GET /bot/taunts` and the taunt push webhook are unchanged. Live events
are additive — they stream during the match; taunts still fire at match
end (`completed` / `timed_out` / `disconnected`). A typical bot handles
both streams.
