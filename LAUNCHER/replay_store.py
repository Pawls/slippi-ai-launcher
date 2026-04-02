"""Parse and cache metadata from Slippi (.slp) replay files."""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import peppi_py

# ── Melee ID mappings ────────────────────────────────────────────────────────

# Slippi external character IDs (NOT Melee internal IDs)
CHAR_NAMES = {
    0: "Captain Falcon", 1: "DK", 2: "Fox", 3: "Game & Watch",
    4: "Kirby", 5: "Bowser", 6: "Link", 7: "Luigi",
    8: "Mario", 9: "Marth", 10: "Mewtwo", 11: "Ness",
    12: "Peach", 13: "Pikachu", 14: "Popo", 15: "Jigglypuff",
    16: "Samus", 17: "Yoshi", 18: "Zelda", 19: "Sheik",
    20: "Falco", 21: "Young Link", 22: "Dr. Mario", 23: "Roy",
    24: "Pichu", 25: "Ganondorf",
}

STAGE_NAMES = {
    2: "Fountain of Dreams", 3: "Pokemon Stadium",
    8: "Yoshi's Story", 28: "Dream Land",
    31: "Battlefield", 32: "Final Destination",
    4: "Peach's Castle", 5: "Kongo Jungle", 6: "Brinstar",
    7: "Corneria", 9: "Onett", 10: "Mute City",
    11: "Rainbow Cruise", 12: "Jungle Japes", 13: "Great Bay",
    14: "Hyrule Temple", 15: "Brinstar Depths", 16: "Yoshi's Island",
    17: "Green Greens", 18: "Fourside", 19: "Mushroom Kingdom",
    20: "Mushroom Kingdom II", 22: "Venom", 23: "Poke Floats",
    24: "Big Blue", 25: "Icicle Mountain", 26: "Icetop",
    27: "Flat Zone", 29: "Yoshi's Island N64", 30: "Kongo Jungle N64",
}

CHAR_ABBREVS = {
    "Captain Falcon": "CFalcon",
    "Game & Watch": "GnW",
    "Jigglypuff": "Puff",
    "Ganondorf": "Ganon",
    "Pikachu": "Pika",
    "Popo": "ICs",
    "Mewtwo": "M2",
    "Young Link": "YLink",
    "Dr. Mario": "Doc",
}

CACHE_FILE = "replay_cache.json"


def _char_name(char_id: int) -> str:
    return CHAR_NAMES.get(char_id, f"Unknown ({char_id})")


def char_abbrev(name: str) -> str:
    """Return abbreviated character name for compact display."""
    return CHAR_ABBREVS.get(name, name)


def _stage_name(stage_id: int) -> str:
    return STAGE_NAMES.get(stage_id, f"Stage {stage_id}")


def normalize_fullwidth(text: str) -> str:
    """Convert fullwidth Unicode characters (U+FF01-FF5E) to ASCII equivalents."""
    if not text:
        return text
    out = []
    for ch in text:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            out.append(chr(cp - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _parse_replay(path: str) -> dict | None:
    """Parse a single .slp file and return metadata dict, or None on error."""
    try:
        game = peppi_py.read_slippi(path, skip_frames=False)
    except Exception:
        return None

    start = game.start
    end = game.end
    frames = game.frames

    # Players
    players = []
    port_index_map = {}  # maps port value -> index in frames.ports
    for idx, p in enumerate(start.players):
        port = p.port.value if hasattr(p.port, "value") else p.port
        port_index_map[port] = idx
        info = {
            "port": port,
            "character": _char_name(p.character),
            "char_id": p.character,
            "type": p.type.name.lower(),
        }
        if p.netplay and p.netplay.name:
            info["name"] = p.netplay.name
        if p.netplay and hasattr(p.netplay, "code") and p.netplay.code:
            info["connect_code"] = p.netplay.code
        if p.name_tag:
            info["name_tag"] = normalize_fullwidth(p.name_tag)
        if p.team is not None:
            info["team"] = p.team.color

        # End-of-game stocks and percent from frame data
        if frames and frames.ports and idx < len(frames.ports):
            port_frames = frames.ports[idx]
            try:
                stocks_arr = port_frames.leader.post.stocks
                percent_arr = port_frames.leader.post.percent
                info["end_stocks"] = int(stocks_arr[-1].as_py())
                info["end_percent"] = round(float(percent_arr[-1].as_py()), 1)
            except Exception:
                pass

        players.append(info)

    # Duration from metadata
    duration_frames = None
    if game.metadata and "lastFrame" in game.metadata:
        duration_frames = game.metadata["lastFrame"]
    elif frames is not None and hasattr(frames, "id"):
        duration_frames = len(frames.id)

    duration_seconds = None
    if duration_frames is not None:
        duration_seconds = round(abs(duration_frames) / 60, 1)

    # Date from metadata or file mtime
    played_at = None
    if game.metadata and "startAt" in game.metadata:
        played_at = game.metadata["startAt"]

    # End method and LRAS detection
    end_method = None
    placements = {}
    lras_port = None
    if end:
        end_method = end.method.name.lower()
        if end.players:
            for pe in end.players:
                port = pe.port.value if hasattr(pe.port, "value") else pe.port
                placements[port] = pe.placement
        if end.lras_initiator is not None:
            lras_port = (end.lras_initiator.value
                         if hasattr(end.lras_initiator, "value")
                         else end.lras_initiator)

    # Determine end type with rage quit detection
    end_type = end_method or "unknown"
    if end_method == "no_contest" and lras_port is not None:
        # Find if the LRAS initiator was losing
        lras_player = None
        other_players = []
        for p in players:
            if p["port"] == lras_port:
                lras_player = p
            else:
                other_players.append(p)

        is_rage_quit = False
        if lras_player and other_players:
            lras_stocks = lras_player.get("end_stocks")
            # Rage quit: LRAS initiator had fewer stocks than any opponent,
            # or same stocks but higher percent
            for opp in other_players:
                opp_stocks = opp.get("end_stocks")
                if lras_stocks is not None and opp_stocks is not None:
                    if lras_stocks < opp_stocks:
                        is_rage_quit = True
                        break
                    if (lras_stocks == opp_stocks
                            and lras_player.get("end_percent", 0)
                            > opp.get("end_percent", 0)):
                        is_rage_quit = True
                        break

        if is_rage_quit:
            end_type = f"rage_quit_p{lras_port + 1}"
        else:
            end_type = f"lras_p{lras_port + 1}"

    # Extra metadata
    console_nick = None
    played_on = None
    if game.metadata:
        console_nick = game.metadata.get("consoleNick")
        played_on = game.metadata.get("playedOn")

    slippi_version = None
    if start.slippi:
        v = start.slippi.version
        slippi_version = f"{v[0]}.{v[1]}.{v[2]}"

    return {
        "path": path,
        "filename": os.path.basename(path),
        "players": players,
        "stage": _stage_name(start.stage),
        "stage_id": start.stage,
        "duration_seconds": duration_seconds,
        "duration_frames": duration_frames,
        "played_at": played_at,
        "end_method": end_method,
        "end_type": end_type,
        "lras_port": lras_port,
        "placements": placements,
        "is_teams": start.is_teams,
        "console_nick": console_nick,
        "played_on": played_on,
        "slippi_version": slippi_version,
        "is_frozen_ps": start.is_frozen_ps,
    }


class ReplayStore:
    """Scans a directory for .slp files and caches parsed metadata."""

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._cache_path = Path(__file__).parent / CACHE_FILE
        self._lock = threading.Lock()
        self._load_cache()

    def _load_cache(self):
        if self._cache_path.exists():
            try:
                with open(self._cache_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._cache = data
            except Exception:
                self._cache = {}

    def _save_cache(self):
        try:
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=1)
        except Exception:
            pass

    def scan(self, replays_dir: str, progress_cb=None) -> list[dict]:
        """Scan replays_dir for .slp files. Returns list of metadata dicts.

        progress_cb(current, total) is called periodically if provided.
        Uses cache for files whose mtime hasn't changed.
        """
        if not replays_dir or not os.path.isdir(replays_dir):
            return []

        slp_files = []
        for root, _dirs, files in os.walk(replays_dir):
            for fname in files:
                if fname.lower().endswith(".slp"):
                    slp_files.append(os.path.join(root, fname))

        results = []
        new_cache = {}
        for i, fpath in enumerate(slp_files):
            if progress_cb and i % 20 == 0:
                progress_cb(i, len(slp_files))

            mtime = os.path.getmtime(fpath)
            cache_key = fpath

            cached = self._cache.get(cache_key)
            if cached and cached.get("_mtime") == mtime:
                new_cache[cache_key] = cached
                results.append(cached)
                continue

            meta = _parse_replay(fpath)
            if meta is None:
                continue

            meta["_mtime"] = mtime

            if not meta.get("played_at"):
                meta["played_at"] = datetime.fromtimestamp(mtime).isoformat()

            new_cache[cache_key] = meta
            results.append(meta)

        with self._lock:
            self._cache = new_cache
            self._save_cache()

        if progress_cb:
            progress_cb(len(slp_files), len(slp_files))

        return results

    def get_cached(self) -> list[dict]:
        """Return previously cached results without rescanning."""
        with self._lock:
            return [v for v in self._cache.values() if "path" in v]
