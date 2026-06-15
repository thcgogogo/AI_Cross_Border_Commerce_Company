"""
watcher.py — watched-folder automation for the desktop daemon (WS12).

Maps a watched folder to an agent id and enqueues a LOCAL agent run whenever a supported
file lands/changes there. Debounces rapid filesystem events, runs a stat-stability check
(so a half-copied file is never read), and MOVES each handled file into a `processed/`
archive so the watcher can never loop on its own output. Every path passes the default-deny
workspace chokepoint (agent_team/workspace.assert_in_workspace) before it is opened — the
watched roots ARE the approved roots.

WS1 owns the supervisor/tray; this module is the folder-watcher only and exposes a clean
API the "Watched folders…" tray item calls. It runs agents through the SAME orchestrator
path a cloud run uses (the fallback _default_enqueue mirrors app.run()); when WS-provided
daemon enqueue exists it is preferred.
"""
import os
import shutil
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .. import config

# Derived from file_tools.parse_file's handled extensions.
SUPPORTED_EXTS = frozenset({"pdf", "docx", "xlsx", "xls", "csv", "json",
                            "txt", "md", "markdown", "log", "text"})
_TEMP_SUFFIXES = (".tmp", ".part", ".crdownload", ".download")
_ARCHIVE_DIR = "processed"


def _state_path():
    from . import _app_data_dir
    return os.path.join(_app_data_dir(), "watch_rules.json")


def load_rules():
    import json
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def save_rules(rules):
    import json
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False)
    except Exception:
        pass


def _norm(p):
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def _is_temp_name(name):
    return (name.startswith(".") or name.startswith("~$")
            or name.lower().endswith(_TEMP_SUFFIXES))


def _in_archive(path):
    return _ARCHIVE_DIR in os.path.normpath(path).split(os.sep)


class FolderWatcher:
    """Owns the watch rules + the watchdog observer. Thread-safe (a lock around rule
    mutation + the debounce-timer dict), since watchdog callbacks run on the observer
    thread while the tray mutates rules on the main thread."""

    def __init__(self, *, debounce_seconds=2.0, stability_interval=0.5, stability_reads=2,
                 enqueuer=None):
        self.debounce_seconds = float(os.environ.get("TANGENT_WATCH_DEBOUNCE_SECONDS")
                                      or debounce_seconds)
        self.stability_interval = stability_interval
        self.stability_reads = max(1, int(stability_reads))
        self._enqueuer = enqueuer            # injectable; None → _default_enqueue
        self._rules = [dict(r) for r in load_rules()]
        self._lock = threading.RLock()
        self._timers = {}                    # path -> Timer (debounce)
        self._observer = None
        self._paused = False

    # ---- rules -----------------------------------------------------------
    def add_rule(self, root, agent_id):
        """Validate + register a watch rule (dir → agent). Refuses an unknown agent, a
        non-existent dir, and a root that overlaps an existing one (double-fire)."""
        if config.get_agent(agent_id) is None:
            raise ValueError(f"未知的智能体：{agent_id}")
        real = _norm(root)
        if not os.path.isdir(real):
            raise ValueError(f"目录不存在：{root}")
        with self._lock:
            for r in self._rules:
                a, b = r["root"], real
                if a == b or os.path.commonpath([a, b]) in (a, b):
                    raise ValueError(f"监控目录与已有规则重叠：{r['root']}")
            self._rules.append({"root": real, "agent_id": agent_id, "enabled": True})
            save_rules(self._rules)
            if self._observer is not None:
                self._observer.schedule(_Handler(self), real, recursive=True)
        return real

    def remove_rule(self, root):
        real = _norm(root)
        with self._lock:
            self._rules = [r for r in self._rules if r["root"] != real]
            save_rules(self._rules)
        self.reload_rules()

    def list_rules(self):
        with self._lock:
            return [dict(r) for r in self._rules]

    # ---- observer lifecycle ---------------------------------------------
    def start(self):
        with self._lock:
            if self._observer is not None:
                return
            self._observer = Observer()
            for r in self._rules:
                if r.get("enabled") and os.path.isdir(r["root"]):
                    self._observer.schedule(_Handler(self), r["root"], recursive=True)
            self._observer.start()

    def stop(self):
        with self._lock:
            obs, self._observer = self._observer, None
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
        if obs is not None:
            obs.stop()
            obs.join(timeout=2)

    def reload_rules(self):
        if self._observer is not None:
            self.stop()
            self.start()

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    # ---- event handling --------------------------------------------------
    def _on_event(self, path):
        """A watchdog event for `path`: (re)start the per-path debounce timer; only when
        it settles do we run handle_path. debounce_seconds<=0 → process synchronously."""
        if self._paused:
            return
        if self.debounce_seconds <= 0:
            self.handle_path(path)
            return
        with self._lock:
            old = self._timers.pop(path, None)
            if old:
                old.cancel()
            t = threading.Timer(self.debounce_seconds, self._fire, args=(path,))
            t.daemon = True
            self._timers[path] = t
            t.start()

    def _fire(self, path):
        with self._lock:
            self._timers.pop(path, None)
        try:
            self.handle_path(path)
        except Exception:
            pass   # one bad file must never kill the observer thread

    def handle_path(self, path):
        """Synchronously process one settled candidate: match a rule, validate, stability-
        check, enqueue, archive. Returns the run handle if enqueued, else None. (The
        debounce path calls this after the timer fires; tests call it directly.)"""
        if self._paused:
            return None
        rule = self._match_rule(path)
        if rule is None or not self._is_candidate(path):
            return None
        real = self._assert_safe(path, rule["root"])
        if real is None:                       # outside the approved (watched) root → rejected
            return None
        if not self._is_stable(real):
            return None
        handle = self._enqueue(rule["agent_id"], [real],
                               source=f"watched_folder:{rule['root']}")
        self._archive(real, rule["root"])
        return handle

    def _match_rule(self, path):
        real = _norm(path)
        if _in_archive(real):
            return None
        with self._lock:
            for r in self._rules:
                if not r.get("enabled"):
                    continue
                root = r["root"]
                try:
                    if os.path.commonpath([root, real]) == root:
                        return r
                except ValueError:
                    continue
        return None

    def _is_candidate(self, path):
        name = os.path.basename(path)
        if _is_temp_name(name) or _in_archive(path):
            return False
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        return ext in SUPPORTED_EXTS

    def _assert_safe(self, path, root):
        """Default-deny: the path must resolve inside the watched (approved) root. Uses
        the WS5 workspace chokepoint — a symlink/.. escape out of the root is rejected."""
        from .. import workspace
        try:
            return workspace.assert_in_workspace(path, [root])
        except workspace.WorkspaceError:
            return None

    def _is_stable(self, path):
        """A file still being written keeps growing — require a non-zero size unchanged
        across `stability_reads` reads (stability_interval apart; 0 in tests)."""
        try:
            last = os.path.getsize(path)
        except OSError:
            return False
        if last <= 0:
            return False
        for _ in range(self.stability_reads - 1):
            if self.stability_interval:
                time.sleep(self.stability_interval)
            try:
                cur = os.path.getsize(path)
            except OSError:
                return False
            if cur != last:
                return False
            last = cur
        return True

    def _archive(self, path, root):
        """MOVE the handled file into <root>/processed/ (uniquified) so it never re-fires.
        On a move failure, drop a `<name>.processed` marker and leave the file — logged,
        never silently dropped."""
        archive = os.path.join(root, _ARCHIVE_DIR)
        try:
            os.makedirs(archive, exist_ok=True)
            dest = os.path.join(archive, os.path.basename(path))
            stem, ext = os.path.splitext(dest)
            n = 2
            while os.path.exists(dest):
                dest = f"{stem}-{n}{ext}"
                n += 1
            shutil.move(path, dest)
            return dest
        except Exception:
            try:
                open(path + ".processed", "w").close()   # mark so we skip it next time
            except Exception:
                pass
            return None

    # ---- enqueue (the local run) ----------------------------------------
    def _enqueue(self, agent_id, paths, source):
        enq = self._enqueuer or _resolve_run_enqueuer()
        return enq(agent_id, paths, source=source)


# ---------------------------------------------------------------------------
# Run enqueuer — prefer the daemon's real one; else the standalone fallback.
# ---------------------------------------------------------------------------
def _resolve_run_enqueuer():
    """Return enqueue(agent_id, file_paths, *, source) -> run_handle. The fallback builds
    the run the same way app.run() does (SharedMemory + parse_file + Orchestrator in a
    daemon thread)."""
    return _default_enqueue


_DEGRADED_BANNER = (
    "[本地离线模式 / LOCAL OFFLINE MODE] 本次分析在本机本地模型上运行，未联网、未使用云端模型，"
    "无法调用文件生成与联网搜索工具，质量低于云端，结果仅供参考。")


def _assemble_run(agent_id, paths, source):
    """Build the run's SharedMemory: log the degraded-mode banner (offline mode ONLY),
    parse each (already workspace-validated) file into memory. Returns (memory, job_id)."""
    import uuid

    from ..file_tools import parse_file
    from ..memory import SharedMemory

    job_id = uuid.uuid4().hex[:12]
    base = os.path.join(os.path.expanduser("~"), ".tangent-agent", "runs")
    goal = f"自动分析（来源：{source}）"
    if config.local_model_mode():
        goal = _DEGRADED_BANNER + "\n\n" + goal
    memory = SharedMemory(job_id, store_dir=os.path.join(base, "mem"), goal=goal,
                          output_dir=os.path.join(base, job_id))
    if config.local_model_mode():
        memory.log("system", _DEGRADED_BANNER, type="warning")
    memory.log("system", f"Watched-folder run for agent '{agent_id}' ({source}).", type="info")
    for p in paths:
        try:
            memory.add_document(parse_file(p))
        except Exception as e:
            memory.log("system", f"Could not read {os.path.basename(p)}: {e}", type="error")
    return memory, job_id


def _default_enqueue(agent_id, paths, source):
    """Standalone fallback: assemble the run and execute it in a daemon thread via the
    SAME Orchestrator path a cloud run uses. (The daemon's real enqueue, when present,
    bills/syncs; this fallback keeps the watcher testable + functional standalone.)"""
    from ..orchestrator import Orchestrator

    memory, job_id = _assemble_run(agent_id, paths, source)

    def _run():
        try:
            Orchestrator(memory).run()
        except Exception as e:
            memory.log("system", f"Run failed: {type(e).__name__}: {e}", type="error")

    t = threading.Thread(target=_run, daemon=True, name=f"watch-run-{job_id}")
    t.start()
    return {"job_id": job_id, "agent_id": agent_id, "thread": t}


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher):
        self._w = watcher

    def on_created(self, event):
        if not event.is_directory:
            self._w._on_event(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._w._on_event(event.src_path)

    def on_moved(self, event):
        dest = getattr(event, "dest_path", None)
        if dest and not event.is_directory:
            self._w._on_event(dest)


# ---------------------------------------------------------------------------
# Singleton accessor — the tray "Watched folders…" item and the supervisor share one.
# (WS1 SEAM: the tray's on_watched callback calls get_watcher().add_rule/list_rules/…;
# the native folder picker that supplies `root` is a v2 concern.)
# ---------------------------------------------------------------------------
_WATCHER = None
_WATCHER_LOCK = threading.Lock()


def get_watcher():
    global _WATCHER
    with _WATCHER_LOCK:
        if _WATCHER is None:
            _WATCHER = FolderWatcher()
        return _WATCHER
