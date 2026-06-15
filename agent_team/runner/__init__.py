"""
agent_team.runner — the local desktop daemon ("Tangent Agent").

Boots the EXISTING Flask app (``app.py``) on a random ``127.0.0.1`` port via
waitress, drops a system-tray icon, and opens the dashboard in the default browser.
In the full product, inference still flows through the cloud ``/v1`` gateway with a
per-device token (other workstreams) — this package is only the process supervisor,
the tray, the loopback hardening, and the path-based run intake.

IMPORTANT: importing this package is SIDE-EFFECT-FREE. Only ``main()`` (or the
``Supervisor`` methods) start a server or a tray, and the heavy deps (waitress,
pystray, the app itself) are imported lazily inside those methods — so the pytest
suite can ``import agent_team.runner`` with no GUI, no server, and no app reload.
"""
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser

__all__ = ["main", "Supervisor", "pick_free_port"]


def pick_free_port():
    """Return a currently-free ephemeral ``127.0.0.1`` port (bind to :0, read it,
    release it). Never hardcode 8000 — the daemon must coexist with a dev server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _default_roots():
    """A sane starting allowlist: the user's Documents/Downloads + the cwd. The
    watched-folders UI (a later workstream) lets the user add more at runtime."""
    home = os.path.expanduser("~")
    cands = [os.path.join(home, "Documents"), os.path.join(home, "Downloads"), os.getcwd()]
    return [c for c in cands if os.path.isdir(c)]


def _app_data_dir():
    """Per-user app-data dir for the daemon's non-secret caches (the agent registry).
    Holds NO device token and no file bytes — only behavior manifests."""
    return os.path.join(os.path.expanduser("~"), ".tangent-agent")


class Supervisor:
    """Owns the daemon lifecycle: env → load app → serve (waitress, bg thread) →
    open browser → run the tray (main thread) → graceful shutdown."""

    def __init__(self, *, port=None, open_browser=True, roots=None):
        self.port = int(port) if port else pick_free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.open_browser = open_browser
        # Per-PROCESS CSRF/origin token, never written to disk. The served index.html
        # carries it so the SPA can echo it on mutating /api/* calls; a cross-origin
        # page cannot read the loopback HTML (CORS) and so cannot forge the token.
        self.csrf_token = secrets.token_urlsafe(32)
        self._roots = roots
        self._app = None
        self._server = None
        self._server_thread = None
        self._icon = None

    # -- the env must be set BEFORE importing app (app reads it at import) -------
    def _prepare_env(self):
        os.environ["LOCAL_MODE"] = "1"
        os.environ["RUN_SCHEDULER"] = "0"   # never start the in-process cron loop
        os.environ["LOCAL_CSRF_TOKEN"] = self.csrf_token

    def _ensure_app_importable(self):
        import agent_team
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(agent_team.__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)

    def load_app(self):
        """Import the Flask app with LOCAL_MODE in the env and seed the workspace
        allowlist. Returns the Flask app object."""
        self._prepare_env()
        self._ensure_app_importable()
        import app as app_module  # importing runs db.init_db() + registers routes
        self._app = app_module.app
        from agent_team import workspace
        roots = self._roots if self._roots is not None else _default_roots()
        workspace.set_roots(roots)
        # Merge any catalog agents this account installed into config.AGENTS BEFORE any
        # run builds an Orchestrator. Best-effort: a registry/network failure must never
        # block boot (the daemon still runs the built-in team).
        try:
            from agent_team import config as _config
            from agent_team.runner import registry
            registry.bootstrap(_config, cache_dir=_app_data_dir())
        except Exception as e:
            print(f"  [i] 智能体目录同步已跳过（{type(e).__name__}: {e}）。")
        return self._app

    def _serve(self):
        from waitress import create_server
        self._server = create_server(self._app, host="127.0.0.1", port=self.port, threads=8)
        try:
            self._server.run()
        except Exception:
            pass   # the server was closed from another thread (Quit) — exit quietly

    def start_server(self):
        if self._app is None:
            self.load_app()
        self._server_thread = threading.Thread(target=self._serve, daemon=True,
                                               name="tangent-waitress")
        self._server_thread.start()
        self._wait_healthz()

    def _wait_healthz(self, timeout=15.0):
        import urllib.request
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.url + "healthz", timeout=1) as r:
                    if getattr(r, "status", r.getcode()) in (200, 503):
                        return True   # 503 = DB degraded but serving; still "up"
            except Exception:
                time.sleep(0.15)
        return False

    def stop(self, *_args):
        """Idempotent shutdown: stop waitress and the tray icon."""
        try:
            if self._server is not None:
                self._server.close()
        except Exception:
            pass
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:
            pass

    def run(self):
        self.start_server()
        print(f"\n  Tangent Agent 正在运行：  {self.url}")
        print("  关闭：托盘菜单选择「退出」，或在此终端按 Ctrl-C。\n")
        if self.open_browser:
            try:
                webbrowser.open(self.url)
            except Exception:
                pass
        # The tray must own the MAIN thread on macOS (the GUI loop). If the tray
        # can't start (headless box / missing deps), degrade to a blocking wait so
        # the daemon still serves — honoring the "label demo, don't fake" rule.
        try:
            from .tray import build_icon
            from . import state
            self._icon = build_icon(
                url=self.url,
                on_open=lambda: webbrowser.open(self.url),
                on_pair=lambda: webbrowser.open(self.url + "account"),   # stub: opens 账户
                on_watched=lambda: print("  [i] 监控文件夹 — 即将推出（暂未实现）。"),
                on_pause_toggle=state.toggle_paused,
                on_quit=self.stop,
            )
            self._icon.run()   # blocks until stop()
        except Exception as e:
            print(f"  [i] 托盘不可用（{type(e).__name__}: {e}）；以无界面模式运行。")
            self._run_headless()

    def _run_headless(self):
        try:
            while self._server_thread and self._server_thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def main(argv=None):
    """Console-script entrypoint (``tangent-agent``). ``--no-browser`` skips the
    auto-open (useful on a headless box)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    sup = Supervisor(open_browser="--no-browser" not in argv)
    try:
        sup.run()
    except KeyboardInterrupt:
        sup.stop()
    return 0
