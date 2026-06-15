"""
test_workspace.py — WS02: the default-deny filesystem chokepoint.

The one gate every local-file read (parse_file) and deliverable write (run_tool) passes
through when the app runs as the local daemon, so a prompt-injected file / attacker path
can't escape the user-approved folders. realpath defeats symlinks; commonpath defeats
`..` and prefix tricks; an empty allowlist denies everything. Opt-in: roots=None →
unrestricted server mode (every existing caller unaffected).
"""
import os
import sys
from io import BytesIO

import pytest
from openpyxl import load_workbook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import crypto, deliverables, workspace  # noqa: E402
from agent_team.file_tools import parse_file  # noqa: E402

_XLSX = {"title": "T", "sheets": [{"name": "S", "headers": ["A", "B"], "rows": [["x", 1]]}]}


# ---------------------------------------------------------------------------
# Core predicate / assert
# ---------------------------------------------------------------------------
def test_deny_all_default():
    assert workspace.is_in_workspace("/anything", []) is False
    assert workspace.is_in_workspace("/anything", None) is False
    with pytest.raises(workspace.WorkspaceViolation):
        workspace.assert_in_workspace("/x", [])


def test_in_root_allows(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("a,b\n1,2\n")
    assert workspace.is_in_workspace(str(f), [str(tmp_path)]) is True
    assert workspace.assert_in_workspace(str(f), [str(tmp_path)]) == os.path.realpath(str(f))


def test_dotdot_traversal_denied(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    assert workspace.is_in_workspace(str(root / ".." / "secret"), [str(root)]) is False


def test_symlink_escape_denied(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    link = root / "link.txt"
    try:
        os.symlink(str(secret), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    assert workspace.is_in_workspace(str(link), [str(root)]) is False     # realpath escapes


def test_prefix_escape_not_allowed(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    evil = tmp_path / "work-evil"
    evil.mkdir()
    # "/…/work-evil/f".startswith("/…/work") is True but is an ESCAPE — commonpath rejects it.
    assert workspace.is_in_workspace(str(evil / "f.txt"), [str(work)]) is False


def test_normalize_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TANGENT_WORKSPACE_ROOTS", os.pathsep.join([str(tmp_path), "", str(tmp_path)]))
    roots = workspace.roots_from_env()
    assert roots == [os.path.realpath(str(tmp_path))]      # blanks dropped, de-duped


# ---------------------------------------------------------------------------
# parse_file enforcement (opt-in)
# ---------------------------------------------------------------------------
def test_parse_file_denies_outside_root_and_propagates(tmp_path):
    inside = tmp_path / "in.csv"
    inside.write_text("a,b\n1,2\n")
    outside = tmp_path.parent / "out.csv"
    outside.write_text("secret")

    # A denial PROPAGATES (it is NOT swallowed into a "[Could not read…]" dict).
    with pytest.raises(workspace.WorkspaceViolation):
        parse_file(str(outside), roots=[str(tmp_path)])
    # In-root read works.
    assert "a" in parse_file(str(inside), roots=[str(tmp_path)])["text"]
    # Server mode (no roots) is unrestricted — the outside file reads fine.
    assert isinstance(parse_file(str(outside)), dict)


# ---------------------------------------------------------------------------
# run_tool enforcement (opt-in, distinct denial)
# ---------------------------------------------------------------------------
def test_run_tool_denies_out_of_workspace_write(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    outside = tmp_path / "outside"          # NOT under root
    res = deliverables.run_tool("create_excel_workbook", _XLSX, str(outside), roots=[str(root)])
    assert res["ok"] is False and res.get("denied") is True
    assert "outside the approved workspace" in res["message"]
    assert not os.path.exists(outside)      # nothing written outside the sandbox


def test_run_tool_allows_in_workspace_write(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    res = deliverables.run_tool("create_excel_workbook", _XLSX, str(root), roots=[str(root)])
    assert res["ok"] is True
    wb = load_workbook(BytesIO(crypto.read_plaintext(res["path"])))
    assert wb["S"]["A1"].value == "A" and wb["S"]["B2"].value == 1


def test_run_tool_unrestricted_without_roots(tmp_path):
    res = deliverables.run_tool("create_excel_workbook", _XLSX, str(tmp_path))   # roots=None
    assert res["ok"] is True and os.path.isfile(res["path"])
