"""FastAPI application factory and shared state."""

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from LAUNCHER.agent_store import AgentStore
from LAUNCHER.config import AppConfig
from LAUNCHER.match_store import MatchStore
from LAUNCHER.replay_store import ReplayStore
from LAUNCHER.resource_store import ResourceStore
from LAUNCHER.tournament_store import TournamentStore


class AppState:
    """Shared application state accessible from route handlers."""

    def __init__(self):
        self.cfg = AppConfig()
        self.agent_store = AgentStore(source="agents")
        self.experiment_store = AgentStore(
            path="experiment_library.json", source="experiments")
        self.match_store = MatchStore()
        self.replay_store = ReplayStore()
        self.resource_store = ResourceStore()
        self.tournament_store = TournamentStore()


# Singleton state, initialized at startup
state: AppState | None = None


def get_state() -> AppState:
    assert state is not None, "App not initialized"
    return state


@asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    state = AppState()

    # Auto-fill paths on startup (same as tkinter app.py)
    from LAUNCHER.config import (
        detect_agents_dir, detect_root,
        slippi_dolphin_dir, slippi_iso, slippi_replays_dir, slippi_user_json,
    )
    cfg = state.cfg
    root = cfg.get("paths", "slippi_ai_root") or detect_root()
    fills = [
        ("slippi_ai_root", lambda: root),
        ("iso", slippi_iso),
        ("dolphin_dir", slippi_dolphin_dir),
        ("user_json", slippi_user_json),
        ("replays_dir", slippi_replays_dir),
        ("agents_dir", lambda: detect_agents_dir(root)),
    ]
    changed = False
    for key, fn in fills:
        if not cfg.get("paths", key):
            v = fn()
            if v:
                cfg.set("paths", key, v)
                changed = True
    if changed:
        cfg.save()

    # Background-warm the pickle parser so the Play screen doesn't pay the
    # TensorFlow import cost on first navigation. This also lazy-fills the
    # cached `names` field on legacy agent records that predate the field.
    threading.Thread(
        target=_prewarm_agent_metadata,
        args=(state,),
        daemon=True,
        name="prewarm-agent-metadata",
    ).start()

    yield

    state = None


def _prewarm_agent_metadata(s: "AppState") -> None:
    """Walk both stores once and ensure every record carries a cached `names`
    list. The first call into ``ensure_names`` will pull TensorFlow into the
    process, after which all subsequent metadata requests are pure dict
    lookups.
    """
    targets = [
        (s.agent_store, s.cfg.get("paths", "agents_dir") or ""),
        (
            s.experiment_store,
            os.path.join(s.cfg.get("paths", "slippi_ai_root") or "", "experiments"),
        ),
    ]
    for store, scan_dir in targets:
        if not scan_dir:
            continue
        try:
            records = store.get_all()
        except Exception:
            continue
        for rec in records:
            if rec.get("names") is not None or rec.get("missing"):
                continue
            full_path = os.path.join(scan_dir, rec.get("agent_path", ""))
            try:
                store.ensure_names(rec["id"], full_path)
            except Exception:
                # Don't let one bad pickle stop the rest of the warmup.
                pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="Slippi AI Launcher API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from LAUNCHER.api.routes import config, agents, matches, tournaments, resources, replays, training, play
    app.include_router(config.router)
    app.include_router(agents.router)
    app.include_router(matches.router)
    app.include_router(tournaments.router)
    app.include_router(resources.router)
    app.include_router(replays.router)
    app.include_router(training.router)
    app.include_router(play.router)

    return app
