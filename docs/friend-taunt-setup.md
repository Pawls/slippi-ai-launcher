# Hooking your Discord bot into match-end taunts (polling)

When a Slippi-AI match ends, the launcher records the outcome (winner,
loser, reason — `completed` / `timed_out` / `disconnected`). You point
your Discord bot at the launcher, poll the event list every few seconds,
and turn each new event into a message in the challenger's channel.
That's how we get the "LOL LOL LOL" when the opponent rage-quits while
losing and the "my b" when the bot loses and bails.

This is the **pull** flow, aimed at you: your bot runs on your machine,
the launcher runs on Paul's, and you reach it over Tailscale. The push
webhook exists too (for a bot running on the same box as the launcher)
but you don't need it.

## TL;DR checklist

1. Get two things from Paul:
   - The **Tailscale URL** of his launcher, e.g. `http://100.x.y.z:8000`
   - The **launcher API token** (shown in the GUI → Play page →
     **Discord Integration** panel, same one you'd use for
     `/bot/launch`).
2. In your bot: every ~3 seconds, hit
   `GET {LAUNCHER_URL}/bot/taunts?since={last_cursor}` with header
   `Authorization: Bearer {LAUNCHER_TOKEN}`.
3. For each event in the response, post a Discord message based on
   `reason` + `winner`. Bump your local `last_cursor` to the `cursor`
   value the response returned so you don't re-process events.
4. Make sure your bot has `View Channel` + `Send Messages` in whatever
   channel ID shows up in the event.

That's the whole protocol.

## The endpoint

**Method / URL:** `GET {LAUNCHER_URL}/bot/taunts?since={int}`

**Headers:**
```
Authorization: Bearer <launcher API token>
```

**Query params:**

| name    | default | meaning                                                   |
|---------|---------|-----------------------------------------------------------|
| `since` | `0`     | Return only events with `id > since`. On first call pass 0 to get everything the launcher still has buffered. |

**Response:**
```json
{
  "events": [
    {
      "id":              42,
      "timestamp":       "2026-04-22T22:14:03.128471+00:00",
      "reason":          "disconnected",
      "winner":          "ai",
      "challenger_id":   "123456789012345678",
      "challenger_tag":  "friend#0001",
      "channel_id":      "987654321098765432"
    }
  ],
  "cursor": 42
}
```

**Fields on each event:**

| field            | values                                           | notes                                                         |
|------------------|--------------------------------------------------|---------------------------------------------------------------|
| `id`             | monotonic int                                    | Filter with `since` to pick up only events you haven't seen.  |
| `timestamp`      | ISO 8601 UTC                                     | When the match ended (server clock).                          |
| `reason`         | `completed` / `timed_out` / `disconnected`       | See table below.                                              |
| `winner`         | `ai` / `human` / `draw` / `null`                 | `null` when no game reached IN_GAME (e.g. `timed_out`).       |
| `challenger_id`  | Discord user ID                                  | The person who issued `/challenge`.                           |
| `challenger_tag` | Discord tag                                      | For `{mention}`-style greetings; could be stale if renamed.   |
| `channel_id`     | Discord channel ID                               | Where to post. Events with no `channel_id` are never emitted. |

**`reason` values:**

| value          | what it means                                           |
|----------------|---------------------------------------------------------|
| `completed`    | Match ended cleanly — one side won.                     |
| `timed_out`    | Challenger never connected within the launch window.    |
| `disconnected` | Somebody left mid-match (rage-quit, crash, LRA+Start).  |

**Cursor convention:** the response's `cursor` is the current max id the
launcher is aware of. Store it locally and pass it as `since` on your
next call. **Don't** persist just `max(event.id)` from the returned
events — if the response is empty, that leaves your cursor stuck. Always
advance to the response's `cursor`.

## Buffer size and missed events

The launcher keeps the most recent **200 events** in memory. If your bot
is offline for long enough that more than 200 matches happen, the
overflow is dropped silently — you'll just skip those. Given a
typical match rate this is never a problem, but the guarantee is
"at-most-once delivery of the last 200 events", not "durable queue". The
buffer is not persisted across launcher restarts.

## Example handler (Python, discord.py)

Drop this into any existing bot. It runs a background task that polls
every 3 seconds and posts a message for each event.

```python
import asyncio, os, random, httpx

LAUNCHER_URL   = os.environ["LAUNCHER_URL"]      # e.g. http://100.x.y.z:8000
LAUNCHER_TOKEN = os.environ["LAUNCHER_TOKEN"]
POLL_INTERVAL  = 3  # seconds

AHEAD_LINES   = ["Lol 😂 GGs I guess", "rage-quit of the century"]
BEHIND_LINES  = ["my b.", "whoops, that one's on me"]
EVEN_LINES    = ["pulled the plug early. I knew you were scared. GGs."]
TIMEOUT_LINES = ["no-showed. I even warmed up. GGs."]

def _pick_line(event: dict) -> str | None:
    reason, winner = event["reason"], event["winner"]
    if reason == "timed_out":
        return random.choice(TIMEOUT_LINES)
    if reason == "disconnected":
        if   winner == "ai":    return random.choice(AHEAD_LINES)
        elif winner == "human": return random.choice(BEHIND_LINES)
        else:                   return random.choice(EVEN_LINES)
    return None  # `completed` → stay silent

async def poll_taunts(client):
    cursor = 0
    headers = {"Authorization": f"Bearer {LAUNCHER_TOKEN}"}
    async with httpx.AsyncClient(timeout=10) as http:
        while not client.is_closed():
            try:
                r = await http.get(
                    f"{LAUNCHER_URL}/bot/taunts",
                    params={"since": cursor},
                    headers=headers,
                )
                r.raise_for_status()
                body = r.json()
            except Exception as e:
                # Tailscale blip, launcher restart, whatever — just retry.
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for event in body.get("events", []):
                line = _pick_line(event)
                if not line:
                    continue
                channel = client.get_channel(int(event["channel_id"]))
                if channel is None:
                    continue
                tag = event.get("challenger_tag") or "GGs"
                try:
                    await channel.send(f"{tag} {line}")
                except Exception:
                    pass  # missing perms, deleted channel, etc.

            cursor = body.get("cursor", cursor)
            await asyncio.sleep(POLL_INTERVAL)

# Kick the task off once the bot is logged in. In a discord.py bot:
@client.event
async def on_ready():
    client.loop.create_task(poll_taunts(client))
```

Required env on your machine:

```
LAUNCHER_URL=http://<Paul's-tailscale-IP>:8000
LAUNCHER_TOKEN=<bearer token from Paul>
```

## One gotcha

The launcher only records events for matches it knows a Discord channel
for — meaning matches launched via the `/bot/launch` API (which carries
`channel_id` through from your `/challenge` handler). Matches started
from the GUI's Play-page Launch button don't have a channel and won't
show up in the event list. This is intentional: there's no channel to
post in for a locally-tested match.

## If you ever run the launcher on your own box

The push webhook is still there. Set the Taunt webhook URL in the GUI to
`http://127.0.0.1:<port>/taunt` and run an inbound listener instead of
polling. Lower latency, less chatter. See `bot.py` in
[caught-slippin](../../caught-slippin) for a reference push handler.
