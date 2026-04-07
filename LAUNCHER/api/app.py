"""FastAPI application factory and shared state."""

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

    yield

    state = None


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

    from LAUNCHER.api.routes import config, agents, matches, tournaments, resources, replays, training
    app.include_router(config.router)
    app.include_router(agents.router)
    app.include_router(matches.router)
    app.include_router(tournaments.router)
    app.include_router(resources.router)
    app.include_router(replays.router)
    app.include_router(training.router)

    return app
