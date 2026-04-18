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
       { "character": "sheik", "style_name": "M2K",   "agent_id": "abc123de" },
       { "character": "sheik", "style_name": "Krudo", "agent_id": "def456gh" }
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
answer "can I play your Sheik as M2K?" before launching.

```json
{
  "state": "available",
  "in_match": false,
  "roster": [
    { "character": "sheik", "styles": ["M2K", "Krudo"] },
    { "character": "fox",   "styles": ["Mang0"] }
  ]
}
```

### `POST /bot/launch`
Request a match. Body:
```json
{
  "challenger_discord_id": "1234567890",
  "challenger_tag": "Pawls#723",
  "connect_code": "PAWL#723",
  "character": "sheik",
  "style_name": "M2K"
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

1. User: *"Yo smash bot, I bet I can beat your Sheik playing like M2K. My code is PAWL#723"*
2. LLM calls `GET /bot/roster` → confirms `sheik` has `M2K` in `styles`.
3. LLM calls `POST /bot/launch` with challenger details.
4. If response is `launching`, LLM replies in Discord: *"Get ready."*
5. If response is `pending_approval`, LLM replies: *"Hold tight — asking Paul."* and polls `GET /bot/challenge/<id>` every few seconds until approved/denied/expired.

## Concurrency contract

Exactly one active match at a time. A second challenge while a match is
running returns 409 `busy` — the LLM should trash-talk and tell the user
to try again later.
