"""Tournament screen: automated round-robin matchups with leaderboard."""

import os
import signal
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from LAUNCHER.agent_store import AgentStore
from LAUNCHER.config import (
    AppConfig, CHARACTERS, STAGES,
    list_agents, extract_delay_from_filename,
    extract_characters_from_filename,
    read_model_delay, read_allowed_characters,
    find_script,
)
from LAUNCHER.match_store import MatchStore
from LAUNCHER.screens import Screen
from LAUNCHER.tournament_store import TournamentStore


# ──────────────────────────────────────────────────────────────────────────────
# Agent picker dialog
# ──────────────────────────────────────────────────────────────────────────────

class _AddAgentDialog(tk.Toplevel):
    """Dialog to pick an agent, character, and delay for the tournament."""

    def __init__(self, parent, cfg: AppConfig, existing_paths: set[str]):
        super().__init__(parent)
        self.title("Add Agent to Tournament")
        self.transient(parent)
        self.resizable(False, False)
        self.result: dict | None = None

        self._cfg = cfg
        self._existing = existing_paths

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        # Agent file
        ttk.Label(frame, text="Agent:").grid(
            row=0, column=0, sticky="w", pady=2)
        self._agent_var = tk.StringVar()
        agents_dir = cfg.get("paths", "agents_dir")
        self._agents_list = list_agents(agents_dir) if agents_dir else []
        # Filter out already-added agents
        self._agents_list = [
            a for a in self._agents_list if a not in existing_paths]
        self._agent_cb = ttk.Combobox(
            frame, textvariable=self._agent_var,
            values=self._agents_list, width=40, state="readonly")
        self._agent_cb.grid(row=0, column=1, padx=(4, 0), pady=2)
        self._agent_cb.bind("<<ComboboxSelected>>", self._on_agent_select)

        # Character
        ttk.Label(frame, text="Character:").grid(
            row=1, column=0, sticky="w", pady=2)
        self._char_var = tk.StringVar()
        self._char_cb = ttk.Combobox(
            frame, textvariable=self._char_var,
            values=CHARACTERS, width=20, state="readonly")
        self._char_cb.grid(row=1, column=1, sticky="w", padx=(4, 0), pady=2)

        # Delay
        ttk.Label(frame, text="Delay:").grid(
            row=2, column=0, sticky="w", pady=2)
        self._delay_var = tk.IntVar(value=2)
        ttk.Spinbox(
            frame, textvariable=self._delay_var, from_=0, to=20, width=5,
        ).grid(row=2, column=1, sticky="w", padx=(4, 0), pady=2)

        # Nickname
        ttk.Label(frame, text="Display name:").grid(
            row=3, column=0, sticky="w", pady=2)
        self._name_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self._name_var, width=30).grid(
            row=3, column=1, sticky="w", padx=(4, 0), pady=2)

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(btn_frame, text="Add", command=self._on_add).pack(
            side="right", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(
            side="right", padx=4)

        self.grab_set()
        self.focus_set()

    def _on_agent_select(self, _event=None):
        agent_path = self._agent_var.get()
        if not agent_path:
            return
        agents_dir = self._cfg.get("paths", "agents_dir")
        full = os.path.join(agents_dir, agent_path)

        # Auto-fill delay
        delay = extract_delay_from_filename(agent_path)
        if delay is None:
            delay = read_model_delay(full)
        if delay is not None:
            self._delay_var.set(delay)

        # Auto-fill character
        chars = extract_characters_from_filename(agent_path)
        if not chars:
            chars = read_allowed_characters(full) or []
        if chars and len(chars) == 1:
            self._char_var.set(chars[0])
        elif not self._char_var.get() and chars:
            self._char_var.set(chars[0])

        # Auto-fill name
        if not self._name_var.get():
            basename = os.path.splitext(os.path.basename(agent_path))[0]
            self._name_var.set(basename)

    def _on_add(self):
        agent_path = self._agent_var.get()
        char = self._char_var.get()
        if not agent_path:
            messagebox.showwarning("Missing", "Select an agent.", parent=self)
            return
        if not char:
            messagebox.showwarning("Missing", "Select a character.", parent=self)
            return
        self.result = {
            "agent_path": agent_path,
            "character": char,
            "delay": self._delay_var.get(),
            "name": self._name_var.get() or os.path.basename(agent_path),
        }
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# Tournament detail screen (run / view results)
# ──────────────────────────────────────────────────────────────────────────────

class _TournamentDetailView(ttk.Frame):
    """Embedded view for a single tournament: leaderboard + matchups + run."""

    def __init__(self, parent, cfg: AppConfig, store: TournamentStore,
                 tid: str, on_back):
        super().__init__(parent)
        self._cfg = cfg
        self._store = store
        self._tid = tid
        self._on_back = on_back
        self._proc: subprocess.Popen | None = None
        self._running = False
        self._cancelled = False

        self._build_ui()
        self._refresh()

    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Button(top, text="\u2190 Back", command=self._on_back).pack(
            side="left")
        self._title_lbl = ttk.Label(
            top, text="", font=("TkDefaultFont", 13, "bold"))
        self._title_lbl.pack(side="left", padx=10)
        self._status_lbl = ttk.Label(top, text="", foreground="gray")
        self._status_lbl.pack(side="left")

        # Action buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=6)
        self._run_btn = tk.Button(
            btn_frame, text="\u25b6  Run Tournament", bg="#4CAF50", fg="white",
            font=("TkDefaultFont", 10, "bold"), cursor="hand2",
            command=self._on_run)
        self._run_btn.pack(side="left", padx=(0, 6))
        self._delete_btn = ttk.Button(
            btn_frame, text="Delete", command=self._on_delete)
        self._delete_btn.pack(side="right")

        # Paned: leaderboard on left, matchups on right
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # ── Leaderboard ──
        lb_frame = ttk.LabelFrame(paned, text="Leaderboard", padding=6)
        paned.add(lb_frame, weight=1)

        lb_cols = ("rank", "name", "character", "w", "l", "d", "pts")
        self._lb_tree = ttk.Treeview(
            lb_frame, columns=lb_cols, show="headings", height=10)
        for col, hdr, w, anchor in [
            ("rank", "#", 30, "center"),
            ("name", "Agent", 140, "w"),
            ("character", "Char", 80, "w"),
            ("w", "W", 40, "center"),
            ("l", "L", 40, "center"),
            ("d", "D", 40, "center"),
            ("pts", "Pts", 50, "center"),
        ]:
            self._lb_tree.heading(col, text=hdr)
            self._lb_tree.column(col, width=w, anchor=anchor, stretch=False)
        self._lb_tree.pack(fill="both", expand=True)

        # ── Matchups ──
        mu_frame = ttk.LabelFrame(paned, text="Matchups", padding=6)
        paned.add(mu_frame, weight=1)

        mu_cols = ("p1", "vs", "p2", "game", "status", "winner")
        self._mu_tree = ttk.Treeview(
            mu_frame, columns=mu_cols, show="headings", height=10)
        for col, hdr, w, anchor in [
            ("p1", "Player 1", 120, "w"),
            ("vs", "", 25, "center"),
            ("p2", "Player 2", 120, "w"),
            ("game", "Game", 45, "center"),
            ("status", "Status", 70, "center"),
            ("winner", "Winner", 120, "w"),
        ]:
            self._mu_tree.heading(col, text=hdr)
            self._mu_tree.column(col, width=w, anchor=anchor, stretch=False)

        mu_scroll = ttk.Scrollbar(
            mu_frame, orient="vertical", command=self._mu_tree.yview)
        self._mu_tree.configure(yscrollcommand=mu_scroll.set)
        self._mu_tree.pack(side="left", fill="both", expand=True)
        mu_scroll.pack(side="right", fill="y")

        # Progress bar
        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", padx=10, pady=(0, 6))
        self._progress_var = tk.DoubleVar(value=0)
        self._progress_bar = ttk.Progressbar(
            prog_frame, variable=self._progress_var, maximum=100)
        self._progress_bar.pack(fill="x", side="left", expand=True)
        self._progress_lbl = ttk.Label(prog_frame, text="", width=16)
        self._progress_lbl.pack(side="right", padx=(6, 0))

        # Color tags for matchup status
        self._mu_tree.tag_configure("done", background="#e8f5e9")
        self._mu_tree.tag_configure("running", background="#fff3e0")
        self._mu_tree.tag_configure("error", background="#ffebee")

    def _refresh(self):
        t = self._store.get_tournament(self._tid)
        if not t:
            return

        self._title_lbl.config(text=t["name"])
        self._status_lbl.config(text=f"[{t['status']}]")

        agents = t["agents"]
        matchups = t["matchups"]

        # Leaderboard
        board = TournamentStore.compute_leaderboard(t)
        self._lb_tree.delete(*self._lb_tree.get_children())
        for i, entry in enumerate(board, 1):
            self._lb_tree.insert("", "end", values=(
                i, entry["name"], entry["character"],
                entry["wins"], entry["losses"], entry["draws"],
                entry["points"],
            ))

        # Matchups
        self._mu_tree.delete(*self._mu_tree.get_children())
        for m in matchups:
            p1 = agents[m["p1_index"]]
            p2 = agents[m["p2_index"]]
            p1_name = p1.get("name") or os.path.basename(p1["agent_path"])
            p2_name = p2.get("name") or os.path.basename(p2["agent_path"])

            winner_name = ""
            if m["status"] == "done":
                wi = m.get("winner_index")
                if wi is not None and 0 <= wi < len(agents):
                    a = agents[wi]
                    winner_name = a.get("name") or os.path.basename(
                        a["agent_path"])
                elif wi is None:
                    winner_name = "Draw"

            tag = m["status"] if m["status"] in ("done", "running", "error") else ""
            self._mu_tree.insert("", "end", values=(
                p1_name, "vs", p2_name,
                m["game_num"], m["status"], winner_name,
            ), tags=(tag,) if tag else ())

        # Progress
        total = len(matchups)
        done = sum(1 for m in matchups if m["status"] in ("done", "error"))
        pct = (done / total * 100) if total else 0
        self._progress_var.set(pct)
        self._progress_lbl.config(text=f"{done}/{total}")

        # Update button state
        if self._running:
            self._run_btn.config(
                text="\u25a0  Stop", bg="red", command=self._on_stop)
        elif t["status"] == "done":
            self._run_btn.config(
                text="\u25b6  Run Again", bg="#4CAF50", command=self._on_run)
        else:
            self._run_btn.config(
                text="\u25b6  Run Tournament", bg="#4CAF50",
                command=self._on_run)

    # ── Run tournament ──────────────────────────────────────────────────

    def _on_run(self):
        t = self._store.get_tournament(self._tid)
        if not t:
            return
        cfg = self._cfg
        root = cfg.get("paths", "slippi_ai_root")
        agents_dir = cfg.get("paths", "agents_dir")
        if not root or not agents_dir:
            messagebox.showerror(
                "Error",
                "Configure slippi-ai root and agents directory in Settings.",
                parent=self)
            return

        script = find_script(root, "scripts/eval_two.py", "eval_two.py")
        if not script:
            messagebox.showerror(
                "Error", "Cannot find eval_two.py in Slippi-AI root.",
                parent=self)
            return

        self._running = True
        self._cancelled = False
        self._store.update_tournament(self._tid, status="running")
        self._refresh()
        threading.Thread(
            target=self._run_matches, args=(t, script, root, agents_dir),
            daemon=True).start()

    def _on_stop(self):
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            self._kill_process(self._proc)
            self._proc = None

    def _run_matches(self, tournament, script, root, agents_dir):
        """Worker thread: run all pending matchups sequentially."""
        agents = tournament["agents"]
        dolphin_path = self._cfg.get("paths", "dolphin_dir")
        iso_path = self._cfg.get("paths", "iso")
        stage = tournament["stage"]

        for matchup in tournament["matchups"]:
            if self._cancelled:
                break
            if matchup["status"] == "done":
                continue

            p1 = agents[matchup["p1_index"]]
            p2 = agents[matchup["p2_index"]]
            p1_pkl = str(Path(agents_dir) / p1["agent_path"])
            p2_pkl = str(Path(agents_dir) / p2["agent_path"])
            delay = max(p1.get("delay", 2), p2.get("delay", 2))

            self._store.update_matchup(
                self._tid, matchup["id"],
                status="running", started=datetime.now().isoformat())
            self.after(0, self._refresh)

            cmd = [
                sys.executable, script,
                f"--dolphin.path={dolphin_path}",
                f"--dolphin.iso={iso_path}",
                f"--dolphin.online_delay={delay}",
                f"--dolphin.stage={stage}",
                f"--dolphin.headless",
                f"--dolphin.infinite_time",
                f"--p1.ai.path={p1_pkl}",
                f"--p1.character={p1['character']}",
                f"--p2.ai.path={p2_pkl}",
                f"--p2.character={p2['character']}",
                "--num_games=1",
            ]

            user_json = self._cfg.get("paths", "user_json")
            if user_json:
                cmd.append(f"--dolphin.user_json_path={user_json}")

            try:
                if sys.platform == "win32":
                    self._proc = subprocess.Popen(
                        cmd, cwd=root,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                else:
                    self._proc = subprocess.Popen(
                        cmd, cwd=root, start_new_session=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

                stdout, _ = self._proc.communicate()
                exit_code = self._proc.returncode
                self._proc = None

                # Parse result from eval output
                winner_index = self._parse_winner(
                    stdout.decode(errors="replace") if stdout else "",
                    matchup["p1_index"], matchup["p2_index"],
                    p1, p2)

                self._store.update_matchup(
                    self._tid, matchup["id"],
                    status="done",
                    winner_index=winner_index,
                    finished=datetime.now().isoformat())

            except Exception:
                self._store.update_matchup(
                    self._tid, matchup["id"],
                    status="error",
                    finished=datetime.now().isoformat())

            self.after(0, self._refresh)

        # Tournament complete
        self._running = False
        status = "paused" if self._cancelled else "done"
        self._store.update_tournament(self._tid, status=status)
        self.after(0, self._refresh)

    def _parse_winner(self, output: str, p1_index: int, p2_index: int,
                      p1: dict, p2: dict) -> int | None:
        """Parse eval_two.py stdout to determine the winner.

        Looks for lines like "Game result: P1 wins" or KO diff patterns.
        Falls back to draw if unparseable.
        """
        output_lower = output.lower()

        # Look for explicit game result lines
        for line in reversed(output.split("\n")):
            ll = line.strip().lower()
            if "p1 wins" in ll or "player 1 wins" in ll:
                return p1_index
            if "p2 wins" in ll or "player 2 wins" in ll:
                return p2_index
            if "draw" in ll and ("result" in ll or "game" in ll):
                return None

        # Look for KO diff patterns (positive = p1 advantage)
        import re
        ko_match = re.search(r"ko[_ ]?diff[:\s]+([+-]?\d+)", output_lower)
        if ko_match:
            diff = int(ko_match.group(1))
            if diff > 0:
                return p1_index
            elif diff < 0:
                return p2_index
            return None

        # If game exited with no parseable result, treat as draw
        return None

    def _kill_process(self, proc):
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _on_delete(self):
        if self._running:
            messagebox.showwarning(
                "Running", "Stop the tournament before deleting.", parent=self)
            return
        if not messagebox.askyesno(
                "Delete", "Delete this tournament?", parent=self):
            return
        self._store.delete_tournament(self._tid)
        self._on_back()

    def cleanup(self):
        """Stop any running process when leaving."""
        if self._running:
            self._on_stop()


# ──────────────────────────────────────────────────────────────────────────────
# Main tournament screen
# ──────────────────────────────────────────────────────────────────────────────

class TournamentScreen(Screen):
    """Round-robin tournament: select agents, run automated matchups, view
    leaderboard results."""

    def __init__(self, parent, navigator, cfg: AppConfig,
                 agent_store: AgentStore, match_store: MatchStore,
                 tournament_store: TournamentStore):
        super().__init__(parent, navigator, cfg)
        self._agent_store = agent_store
        self._match_store = match_store
        self._store = tournament_store
        self._detail_view: _TournamentDetailView | None = None

        self._build_list_view()

    def _build_list_view(self):
        self._list_frame = ttk.Frame(self)
        self._list_frame.pack(fill="both", expand=True)

        self._add_back_button(self._list_frame)

        ttk.Label(self._list_frame, text="Tournaments",
                  font=("TkDefaultFont", 14, "bold")).pack(
            anchor="w", padx=10, pady=(4, 0))

        # Action bar
        action = ttk.Frame(self._list_frame)
        action.pack(fill="x", padx=10, pady=6)
        tk.Button(
            action, text="+ New Tournament", bg="#4CAF50", fg="white",
            font=("TkDefaultFont", 10, "bold"), cursor="hand2",
            command=self._new_tournament,
        ).pack(side="left")

        # Tournament list
        cols = ("name", "agents", "games", "stage", "status", "created")
        self._list_tree = ttk.Treeview(
            self._list_frame, columns=cols, show="headings", height=12)
        for col, hdr, w in [
            ("name", "Name", 180),
            ("agents", "Agents", 60),
            ("games", "Matchups", 80),
            ("stage", "Stage", 130),
            ("status", "Status", 80),
            ("created", "Created", 140),
        ]:
            self._list_tree.heading(col, text=hdr)
            self._list_tree.column(col, width=w, anchor="w")
        self._list_tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._list_tree.bind("<Double-1>", self._on_open)

        # Detail container (hidden initially)
        self._detail_frame = ttk.Frame(self)

    def _populate_list(self):
        self._list_tree.delete(*self._list_tree.get_children())
        for t in self._store.get_all():
            created = ""
            try:
                dt = datetime.fromisoformat(t["created"])
                created = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                pass
            self._list_tree.insert("", "end", iid=t["id"], values=(
                t["name"],
                len(t["agents"]),
                len(t["matchups"]),
                t["stage"],
                t["status"],
                created,
            ))

    def on_enter(self):
        if self._detail_view:
            return
        self._populate_list()

    def on_leave(self):
        if self._detail_view:
            self._detail_view.cleanup()

    # ── New tournament wizard ───────────────────────────────────────────

    def _new_tournament(self):
        wizard = _NewTournamentWizard(self, self.cfg, self._store)
        self.wait_window(wizard)
        if wizard.created_tid:
            self._populate_list()
            self._open_tournament(wizard.created_tid)

    # ── Open tournament detail ──────────────────────────────────────────

    def _on_open(self, _event=None):
        sel = self._list_tree.selection()
        if not sel:
            return
        self._open_tournament(sel[0])

    def _open_tournament(self, tid: str):
        self._list_frame.pack_forget()

        if self._detail_view:
            self._detail_view.cleanup()
            self._detail_view.destroy()

        self._detail_view = _TournamentDetailView(
            self._detail_frame, self._cfg, self._store, tid,
            on_back=self._close_detail)
        self._detail_view.pack(fill="both", expand=True)
        self._detail_frame.pack(fill="both", expand=True)

    def _close_detail(self):
        if self._detail_view:
            self._detail_view.cleanup()
            self._detail_view.destroy()
            self._detail_view = None
        self._detail_frame.pack_forget()
        self._list_frame.pack(fill="both", expand=True)
        self._populate_list()


# ──────────────────────────────────────────────────────────────────────────────
# New tournament wizard
# ──────────────────────────────────────────────────────────────────────────────

class _NewTournamentWizard(tk.Toplevel):
    """Modal dialog to configure and create a new tournament."""

    def __init__(self, parent, cfg: AppConfig, store: TournamentStore):
        super().__init__(parent)
        self.title("New Tournament")
        self.transient(parent)
        self.resizable(True, True)
        self.minsize(500, 400)
        self._cfg = cfg
        self._store = store
        self.created_tid: str | None = None
        self._agents: list[dict] = []

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        # Name
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Name:").pack(side="left")
        self._name_var = tk.StringVar(
            value=f"Tournament {datetime.now().strftime('%m/%d %H:%M')}")
        ttk.Entry(row, textvariable=self._name_var, width=30).pack(
            side="left", padx=(4, 0))

        # Stage
        row2 = ttk.Frame(frame)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Stage:").pack(side="left")
        self._stage_var = tk.StringVar(value="FINAL_DESTINATION")
        ttk.Combobox(
            row2, textvariable=self._stage_var, values=STAGES,
            width=25, state="readonly").pack(side="left", padx=(4, 0))

        # Games per matchup
        row3 = ttk.Frame(frame)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="Games per matchup:").pack(side="left")
        self._num_games_var = tk.IntVar(value=3)
        ttk.Spinbox(
            row3, textvariable=self._num_games_var, from_=1, to=20, width=5,
        ).pack(side="left", padx=(4, 0))

        # Agents
        agent_lf = ttk.LabelFrame(frame, text="Agents", padding=6)
        agent_lf.pack(fill="both", expand=True, pady=(8, 0))

        agent_btn_frame = ttk.Frame(agent_lf)
        agent_btn_frame.pack(fill="x")
        ttk.Button(
            agent_btn_frame, text="+ Add Agent",
            command=self._add_agent).pack(side="left")
        ttk.Button(
            agent_btn_frame, text="Remove Selected",
            command=self._remove_agent).pack(side="left", padx=4)

        cols = ("name", "character", "delay", "path")
        self._agent_tree = ttk.Treeview(
            agent_lf, columns=cols, show="headings", height=6)
        for col, hdr, w in [
            ("name", "Name", 140),
            ("character", "Character", 80),
            ("delay", "Delay", 50),
            ("path", "Path", 250),
        ]:
            self._agent_tree.heading(col, text=hdr)
            self._agent_tree.column(col, width=w, anchor="w")
        self._agent_tree.pack(fill="both", expand=True, pady=(4, 0))

        # Info label
        self._info_lbl = ttk.Label(
            frame, text="Add at least 2 agents to create a tournament.",
            foreground="gray")
        self._info_lbl.pack(anchor="w", pady=(4, 0))

        # Bottom buttons
        bot = ttk.Frame(frame)
        bot.pack(fill="x", pady=(8, 0))
        ttk.Button(bot, text="Create", command=self._on_create).pack(
            side="right", padx=4)
        ttk.Button(bot, text="Cancel", command=self.destroy).pack(
            side="right", padx=4)

        self.grab_set()
        self.focus_set()

    def _add_agent(self):
        existing = {a["agent_path"] for a in self._agents}
        dlg = _AddAgentDialog(self, self._cfg, existing)
        self.wait_window(dlg)
        if dlg.result:
            self._agents.append(dlg.result)
            self._refresh_agents()

    def _remove_agent(self):
        sel = self._agent_tree.selection()
        if not sel:
            return
        indices = set()
        for iid in sel:
            try:
                indices.add(int(iid))
            except ValueError:
                pass
        self._agents = [
            a for i, a in enumerate(self._agents) if i not in indices]
        self._refresh_agents()

    def _refresh_agents(self):
        self._agent_tree.delete(*self._agent_tree.get_children())
        for i, a in enumerate(self._agents):
            self._agent_tree.insert("", "end", iid=str(i), values=(
                a["name"], a["character"], a["delay"], a["agent_path"],
            ))
        n = len(self._agents)
        if n < 2:
            self._info_lbl.config(
                text="Add at least 2 agents to create a tournament.")
        else:
            from math import comb
            total = comb(n, 2) * self._num_games_var.get()
            self._info_lbl.config(
                text=f"{n} agents \u2192 {comb(n, 2)} pairings "
                     f"\u00d7 {self._num_games_var.get()} games = "
                     f"{total} matches")

    def _on_create(self):
        if len(self._agents) < 2:
            messagebox.showwarning(
                "Need agents",
                "Add at least 2 agents to create a tournament.",
                parent=self)
            return
        name = self._name_var.get().strip() or "Unnamed Tournament"
        tid = self._store.create_tournament(
            name=name,
            agents=self._agents,
            stage=self._stage_var.get(),
            num_games=self._num_games_var.get(),
        )
        self.created_tid = tid
        self.destroy()
