"""Live mid-match gamestate reactions.

Detectors run inside the netplay subprocess on per-frame action-state
tuples and emit ``LiveEvent``s describing notable opponent behavior
(shield-grab spam, roll spam, ledge camping, same-smash spam). The
observer thread dispatches events to the launcher via HTTP; the launcher
attaches match context and fans them out to the Discord bot(s).

See ``scripts/netplay.py`` for the tap-in point and
``LAUNCHER/api/routes/bot.py`` for the delivery endpoints.
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
from slippi_ai.live_events.observer import LiveEventObserver

__all__ = [
    "LiveEvent",
    "ShieldGrabDetector",
    "RollSpamDetector",
    "LedgeCampDetector",
    "SmashSpamDetector",
    "build_detectors",
    "DETECTOR_DEFAULTS",
    "LiveEventObserver",
]
