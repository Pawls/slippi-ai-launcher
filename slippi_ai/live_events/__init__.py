"""Live mid-match gamestate reactions.

Pure detector classes that consume per-frame ``(port, action, frame)``
tuples and emit ``LiveEvent``s describing notable opponent behavior
(shield-grab spam, roll spam, ledge camping, same-smash spam).

Detection runs in the launcher process: ``scripts/netplay.py`` prints
``[LIVE_EVENT_FRAME]`` sentinels when the opponent's action-state
changes; ``LAUNCHER/api/routes/bot.py`` parses them, feeds the
detectors here, and dispatches emitted events to the configured
Discord bot. Keeping detection out of the netplay subprocess avoids
GIL contention with the AI agent's async-inference thread.
"""

from slippi_ai.live_events.detectors import (
    LiveEvent,
    ShieldGrabDetector,
    RollSpamDetector,
    LedgeCampDetector,
    SmashSpamDetector,
    build_detectors,
    DETECTOR_DEFAULTS,
)

__all__ = [
    "LiveEvent",
    "ShieldGrabDetector",
    "RollSpamDetector",
    "LedgeCampDetector",
    "SmashSpamDetector",
    "build_detectors",
    "DETECTOR_DEFAULTS",
]
