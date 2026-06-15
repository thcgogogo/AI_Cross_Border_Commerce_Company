"""
test_runner_watcher.py — WS12: the watched-folder automation.

A supported file dropped into a watched root, once settled, enqueues exactly ONE local run
and is moved into processed/ (no loop). Unsupported/temp/processed/symlink-escape files are
ignored. Hermetic: an injected enqueuer (no real LLM), $HOME redirected to tmp so the rule
store + run dirs stay isolated, and fast/zero debounce+stability windows.
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team.runner import watcher as W  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))           # rule store + run dirs under tmp
    monkeypatch.delenv("LOCAL_MODEL_MODE", raising=False)


def _watcher(calls):
    def enq(agent_id, paths, source):
        calls.append((agent_id, list(paths), source))
        return {"job_id": "test"}
    return W.FolderWatcher(debounce_seconds=0, stability_interval=0, stability_reads=1,
                           enqueuer=enq)


def _root(tmp_path):
    r = tmp_path / "watch"
    r.mkdir()
    return r


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
def test_add_rule_validation(tmp_path):
    w = _watcher([])
    root = _root(tmp_path)
    with pytest.raises(ValueError):
        w.add_rule(str(root), "no_such_agent")
    with pytest.raises(ValueError):
        w.add_rule(str(tmp_path / "ghost"), "data_analyst")
    w.add_rule(str(root), "data_analyst")
    assert w.list_rules()[0]["agent_id"] == "data_analyst"
    # A nested/overlapping root is refused.
    (root / "sub").mkdir()
    with pytest.raises(ValueError):
        w.add_rule(str(root / "sub"), "data_analyst")


# ---------------------------------------------------------------------------
# the handle pipeline
# ---------------------------------------------------------------------------
def test_supported_file_enqueues_once_and_archives(tmp_path):
    calls = []
    w = _watcher(calls)
    root = _root(tmp_path)
    w.add_rule(str(root), "data_analyst")
    f = root / "sales.csv"
    f.write_text("region,sales\n华东,120\n", encoding="utf-8")

    w.handle_path(str(f))
    assert len(calls) == 1
    assert calls[0][0] == "data_analyst" and calls[0][2] == f"watched_folder:{os.path.realpath(str(root))}"
    # Moved into processed/ — the original is gone, so it can never re-fire.
    assert not f.exists()
    assert list((root / "processed").glob("sales*.csv"))


def test_unsupported_file_ignored(tmp_path):
    calls = []
    w = _watcher(calls)
    root = _root(tmp_path)
    w.add_rule(str(root), "data_analyst")
    z = root / "archive.zip"
    z.write_bytes(b"PK\x03\x04")
    assert w.handle_path(str(z)) is None
    assert not calls and z.exists()           # not enqueued, not moved


def test_temp_and_processed_files_ignored(tmp_path):
    calls = []
    w = _watcher(calls)
    root = _root(tmp_path)
    w.add_rule(str(root), "data_analyst")
    (root / "~$open.xlsx").write_text("x")
    (root / "partial.csv.tmp").write_text("x")
    proc = root / "processed"
    proc.mkdir()
    (proc / "done.csv").write_text("a,b\n1,2\n")
    for p in (root / "~$open.xlsx", root / "partial.csv.tmp", proc / "done.csv"):
        assert w.handle_path(str(p)) is None
    assert not calls


def test_symlink_escape_rejected(tmp_path):
    calls = []
    w = _watcher(calls)
    root = _root(tmp_path)
    w.add_rule(str(root), "data_analyst")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.csv"
    secret.write_text("a,b\n1,2\n")
    link = root / "link.csv"
    try:
        os.symlink(str(secret), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert w.handle_path(str(link)) is None    # realpath escapes the root → rejected
    assert not calls


def test_pause_blocks_then_resume(tmp_path):
    calls = []
    w = _watcher(calls)
    root = _root(tmp_path)
    w.add_rule(str(root), "data_analyst")
    f = root / "a.csv"
    f.write_text("a,b\n1,2\n")
    w.pause()
    assert w.handle_path(str(f)) is None and not calls
    w.resume()
    w.handle_path(str(f))
    assert len(calls) == 1


def test_start_stop_idempotent(tmp_path):
    w = _watcher([])
    w.add_rule(str(_root(tmp_path)), "data_analyst")
    w.start(); w.start()      # idempotent
    w.stop(); w.stop()


def test_debounce_collapses_rapid_events(tmp_path):
    calls = []
    w = W.FolderWatcher(debounce_seconds=0.1, stability_interval=0, stability_reads=1,
                        enqueuer=lambda a, p, source: calls.append(1))
    root = _root(tmp_path)
    w.add_rule(str(root), "data_analyst")
    f = root / "big.csv"
    f.write_text("a,b\n1,2\n")
    for _ in range(5):                         # many events for one logical save
        w._on_event(str(f))
    deadline = time.time() + 2
    while time.time() < deadline and not calls:
        time.sleep(0.02)
    assert len(calls) == 1                      # debounced to a single enqueue


# ---------------------------------------------------------------------------
# degraded-mode labelling (offline mode only)
# ---------------------------------------------------------------------------
def test_degraded_banner_only_in_local_mode(tmp_path, monkeypatch):
    import json
    f = tmp_path / "x.csv"
    f.write_text("a,b\n1,2\n")

    monkeypatch.setenv("LOCAL_MODEL_MODE", "1")
    mem, _ = W._assemble_run("data_analyst", [str(f)], "watched_folder:x")
    assert "LOCAL OFFLINE MODE" in json.dumps(mem.snapshot(), ensure_ascii=False)

    monkeypatch.delenv("LOCAL_MODEL_MODE", raising=False)
    mem2, _ = W._assemble_run("data_analyst", [str(f)], "watched_folder:x")
    assert "LOCAL OFFLINE MODE" not in json.dumps(mem2.snapshot(), ensure_ascii=False)


def test_get_watcher_singleton():
    assert W.get_watcher() is W.get_watcher()
