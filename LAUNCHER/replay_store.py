"""Parse and cache metadata from Slippi (.slp) replay files."""

import json
import os
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    import numpy as np
    import peppi_py
    _HAS_REPLAY_DEPS = True
except ImportError:
    _HAS_REPLAY_DEPS = False

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


# ── Box controller detection ────────────────────────────────────────────────

# Box controllers (B0XX, Frame1, etc.) digitize stick input into a small set
# of discrete coordinates.  GCC analog sticks produce continuous values with
# per-frame noise, yielding hundreds of unique (x, y) pairs in a typical game.
# We classify by counting unique joystick coordinate pairs.

_GCC_UNIQUE_THRESHOLD = 80   # more unique pairs than this → definitely GCC
_BOX_UNIQUE_THRESHOLD = 40   # fewer than this after enough frames → box
_BOX_MIN_FRAMES = 600        # ~10 s of gameplay before concluding "box"
_CHUNK_SIZE = 500             # frames per analysis chunk


def _detect_input_type(port_frames) -> str | None:
    """Analyse joystick data for one player and return 'Box' or 'GCC'.

    Returns None if there is insufficient data to classify.
    Uses chunked processing with early exit for GCC only (many unique
    pairs appear quickly).  Box classification requires processing all
    frames because idle game-start sections can look like a box even
    on a GCC.
    """
    try:
        pre = port_frames.leader.pre
        jx = pre.joystick.x.to_numpy()
        jy = pre.joystick.y.to_numpy()
    except Exception:
        return None

    n = len(jx)
    if n < _BOX_MIN_FRAMES:
        return None  # too short to classify reliably

    seen: set[tuple[float, float]] = set()
    for start in range(0, n, _CHUNK_SIZE):
        end = min(start + _CHUNK_SIZE, n)
        chunk = np.round(
            np.column_stack((jx[start:end], jy[start:end])), 3)
        seen.update(map(tuple, chunk))

        if len(seen) > _GCC_UNIQUE_THRESHOLD:
            return "GCC"

    return "Box" if len(seen) <= _BOX_UNIQUE_THRESHOLD else "GCC"


def _parse_replay(path: str, *, detect_box: bool = False) -> dict | None:
    """Parse a single .slp file and return metadata dict, or None on error."""
    try:
        game = peppi_py.read_slippi(path, skip_frames=False)
    except Exception:
        return None

    start = game.start
    end = game.end
    frames = game.frames

    # Player names from metadata (older Slippi versions store names here)
    meta_players = {}
    if game.metadata and "players" in game.metadata:
        meta_players = game.metadata["players"]

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

        # Try start.players[].netplay first (newer replays),
        # then fall back to metadata.players.{port}.names (older replays)
        meta_names = meta_players.get(str(port), {}).get("names", {})

        name = ""
        if p.netplay and p.netplay.name:
            name = p.netplay.name
        elif meta_names.get("netplay"):
            name = meta_names["netplay"]
        if name:
            info["name"] = name

        code = ""
        if p.netplay and hasattr(p.netplay, "code") and p.netplay.code:
            code = p.netplay.code
        elif meta_names.get("code"):
            code = meta_names["code"]
        if code:
            info["connect_code"] = code

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

            if detect_box:
                input_type = _detect_input_type(port_frames)
                if input_type:
                    info["input_type"] = input_type

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

    try:
        file_size = os.path.getsize(path)
    except OSError:
        file_size = None

    return {
        "path": path,
        "filename": os.path.basename(path),
        "file_size": file_size,
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

    def scan(self, replays_dir: str, progress_cb=None,
             detect_box: bool = False,
             cancel_event: threading.Event | None = None) -> list[dict]:
        """Scan replays_dir for .slp files. Returns list of metadata dicts.

        progress_cb(current, total) is called periodically if provided.
        Uses cache for files whose mtime hasn't changed.
        When detect_box is True, cached entries missing input_type data
        are re-parsed to add controller classification.
        Uncached files are parsed in parallel using multiple processes.
        If cancel_event is set, the scan stops early and returns partial
        results (still cached).
        """
        if not _HAS_REPLAY_DEPS:
            return []
        if not replays_dir or not os.path.isdir(replays_dir):
            return []

        slp_files = []
        for root, _dirs, files in os.walk(replays_dir):
            for fname in files:
                if fname.lower().endswith(".slp"):
                    slp_files.append(os.path.join(root, fname))

        total = len(slp_files)

        # Separate cached hits from files that need parsing
        new_cache = {}
        results = []
        to_parse = []  # (fpath, mtime) pairs
        for fpath in slp_files:
            mtime = os.path.getmtime(fpath)
            cached = self._cache.get(fpath)
            if cached and cached.get("_mtime") == mtime:
                needs_box = (detect_box and cached.get("players")
                             and not any(p.get("input_type")
                                         for p in cached["players"]))
                if not needs_box:
                    new_cache[fpath] = cached
                    results.append(cached)
                    continue
            to_parse.append((fpath, mtime))

        # Report cached files as immediate progress
        done = len(results)
        if progress_cb and total > 0:
            progress_cb(done, total)

        # Parse uncached files in parallel
        cancelled = False
        if to_parse:
            max_workers = max((os.cpu_count() or 1) // 2, 1)
            workers = min(len(to_parse), max_workers)
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_parse_replay, fpath, detect_box=detect_box):
                        (fpath, mtime)
                    for fpath, mtime in to_parse
                }
                for future in as_completed(futures):
                    if cancel_event and cancel_event.is_set():
                        # Cancel remaining futures and stop
                        for f in futures:
                            f.cancel()
                        cancelled = True
                        break

                    fpath, mtime = futures[future]
                    try:
                        meta = future.result()
                    except Exception:
                        meta = None

                    if meta is not None:
                        meta["_mtime"] = mtime
                        if not meta.get("played_at"):
                            meta["played_at"] = (
                                datetime.fromtimestamp(mtime).isoformat())
                        new_cache[fpath] = meta
                        results.append(meta)

                    done += 1
                    if progress_cb and done % 20 == 0:
                        progress_cb(done, total)

        with self._lock:
            self._cache = new_cache
            self._save_cache()

        if progress_cb and not cancelled:
            progress_cb(total, total)

        return results

    def get_cached(self) -> list[dict]:
        """Return previously cached results without rescanning."""
        with self._lock:
            return [v for v in self._cache.values() if "path" in v]

    def remove(self, paths: list[str]) -> int:
        """Drop the given paths from the cache. Returns number removed."""
        removed = 0
        with self._lock:
            for p in paths:
                if p in self._cache:
                    del self._cache[p]
                    removed += 1
            if removed > 0:
                self._save_cache()
        return removed

    def has_input_type_data(self) -> bool:
        """Check if cached replays already have input_type detection data."""
        with self._lock:
            replays = [v for v in self._cache.values() if "path" in v]
            if not replays:
                return True  # nothing cached, nothing to rescan
            return any(
                p.get("input_type")
                for r in replays
                for p in r.get("players", [])
            )
