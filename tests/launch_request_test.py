"""LaunchRequest connect_code normalization.

The Discord bot's `/bot/launch` endpoint forwards whatever the
challenger typed — lowercase codes like ``pawl#566`` silently fail to
connect because Slippi matchmaking is case-sensitive. The pydantic
validator on ``LaunchRequest.connect_code`` is the single chokepoint
that normalizes inbound, queue-promoted, and approval-flow launches.
"""

import unittest

from LAUNCHER.api.routes.bot import LaunchRequest


def _kwargs(connect_code: str) -> dict:
    """Minimal valid LaunchRequest kwargs with the given connect code."""
    return dict(
        challenger_discord_id="123",
        connect_code=connect_code,
        character="fox",
        style_name="Vex",
    )


class LaunchRequestConnectCodeTest(unittest.TestCase):

    def test_lowercase_uppercased(self):
        r = LaunchRequest(**_kwargs("pawl#566"))
        self.assertEqual(r.connect_code, "PAWL#566")

    def test_already_uppercase_unchanged(self):
        r = LaunchRequest(**_kwargs("TSC#007"))
        self.assertEqual(r.connect_code, "TSC#007")

    def test_mixed_case_uppercased(self):
        r = LaunchRequest(**_kwargs("MiX#042"))
        self.assertEqual(r.connect_code, "MIX#042")

    def test_whitespace_stripped(self):
        r = LaunchRequest(**_kwargs("  vex#042  "))
        self.assertEqual(r.connect_code, "VEX#042")

    def test_empty_string_stays_empty(self):
        # Queue promotion reuses stored entries; an entry saved pre-fix
        # could have an empty string. Validator must not raise on empty.
        r = LaunchRequest(**_kwargs(""))
        self.assertEqual(r.connect_code, "")

    def test_whitespace_only_becomes_empty(self):
        r = LaunchRequest(**_kwargs("   "))
        self.assertEqual(r.connect_code, "")


if __name__ == "__main__":
    unittest.main()
