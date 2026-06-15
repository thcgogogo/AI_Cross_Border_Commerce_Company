<!-- WS02 · Phase 2 · depends_on: WS1 -->
# WS02 — Filesystem allowlist chokepoint

> **Phase 2** · **Depends on:** WS1 · **Key files:** agent_team/workspace.py (new), agent_team/file_tools.py, agent_team/deliverables.py

---


## Objective
Add a single, default-deny filesystem chokepoint that every local-file read and every deliverable write must pass through, so that when this app runs as a tray-resident LOCAL DAEMON driven by remote/cloud-orchestrated agent jobs, a prompt-injected spreadsheet (or a malicious/path-traversal `agent_id`/filename) cannot escape the user-approved folders to read `~/.ssh`, write outside the chosen output folder, or follow a symlink out of the sandbox.

Concretely: create `agent_team/workspace.py` exposing `assert_in_workspace(path, roots)` that canonicalizes with `os.path.realpath` + `os.path.commonpath` and rejects anything not strictly inside one of `roots`. Wire it in at the TOP of `file_tools.parse_file` (before any file is opened) and inside `deliverables.run_tool` (before the builder writes / before any `open()`). The default roots are `[]` (deny-all): nothing is readable/writable unless an explicit allowlist is passed in. This is a pure-security-control workstream — no new endpoints, no new tables, no daemon code.

This is **Phase 2 / Workstream WS2**. It depends on WS1 (which introduces `LOCAL_MODE` / the runner scaffold). You do NOT need WS1 to be merged to do this work — WS2 is self-contained library code plus call-site wiring. Do not implement the daemon, pairing, device tokens, catalog sync, or the path-aware `/api/run` variant here; those are other workstreams.

## Read these first (exact files/functions, real paths)
1. `/Users/brucetang/Company_Agent_Demo/agent_team/file_tools.py` — `parse_file(path)` (line 22). This is THE single normalizer that turns one on-disk file into text (xlsx/csv/docx/pdf/json/txt). Every reader (`_read_pdf`, `_read_docx`, `_read_spreadsheet`, `_read_json`, `_read_text`) opens `path` directly. Note the existing design rule: "never let one bad file kill the batch" — `parse_file` wraps readers in try/except and returns an error-shaped dict rather than raising. Your security check must NOT be swallowed by that try/except into a soft "[Could not read…]" string — see "Failure semantics" below.
2. `/Users/brucetang/Company_Agent_Demo/agent_team/deliverables.py` — `run_tool(name, tool_input, out_dir)` (line 204) and the `_BUILDERS` table (line 383). `run_tool` builds the final path via `_unique(out_dir, _safe_filename(base, ext))` then calls `builder(tool_input, path)` then `crypto.encrypt_file_in_place(path)`. The builders open files for write (`prs.save(path)`, `doc.build(...)`, `wb.save(path)`, `open(path, "w")`). Note `_safe_filename` already runs `werkzeug.utils.secure_filename` (strips `/`, `..`), but `out_dir` itself is attacker-influenceable in the local-daemon design (it comes from the user-picked folder / job payload), so the chokepoint must validate the RESOLVED final path against approved roots regardless.
3. `/Users/brucetang/Company_Agent_Demo/agent_team/crypto.py` — `encrypt_file_in_place(path)` (line 84). Encryption-at-rest is a no-op when `FERNET_KEYS` is unset (dev) and idempotent. Your change must not break this; the workspace check runs BEFORE the write, encryption stays exactly where it is (after the write).
4. `/Users/brucetang/Company_Agent_Demo/agent_team/base_agent.py` lines 86–96 — the live caller: `out_dir = self.memory.output_dir()` then `deliverables.run_tool(name, tool_input, out_dir)`. This is how `run_tool` is invoked today on the server; it must keep working unchanged when no roots are configured (server mode = unrestricted, see "Server-mode compatibility").
5. `/Users/brucetang/Company_Agent_Demo/agent_team/config.py` lines 127–147 — the env-flag conventions: `_TRUE = ("1","true","yes","on")`, `_env(name)` returns a stripped env var or `""`. Follow this exact style for any new config accessor you add.
6. `/Users/brucetang/Company_Agent_Demo/tests/test_deliverables.py` — the established test style for this area (drive `deliverables.run_tool` with `tmp_path`, read bytes back through `crypto.read_plaintext`). Mirror its structure (project-root `sys.path` insert at top, `import os, sys`, `from agent_team import ...`).
7. `/Users/brucetang/Company_Agent_Demo/tests/test_phase8_security.py` — the security-suite style (pytest, `tmp_path`, `monkeypatch.setenv`). Your new tests should read like these.

Callers of `parse_file` (for awareness only — do NOT change their signatures in this workstream): `app.py:858`, `app.py:1138`, `app.py:3641`, `app.py:4127`. These are server-side upload paths that already write the file into a server-controlled temp/store dir, so under the default deny-all they must continue to work — which they will, because server callers pass no roots and you will make "no roots configured" mean "skip the check" in the explicit server path (see "Server-mode compatibility").

## Implementation spec

### 1. New file: `agent_team/workspace.py`
Create a small, dependency-free module (stdlib `os` only) with a module docstring stating the threat model in plain language: "Default-deny filesystem gate. In LOCAL_MODE the agent brain is driven by remote/untrusted input (prompt-injected files, attacker-influenced paths); this is the one chokepoint that guarantees file access stays inside the folders the human explicitly picked. realpath() defeats symlink escapes; commonpath() defeats `..` traversal; an empty allowlist denies everything."

Public surface:

```python
class WorkspaceViolation(Exception):
    """Raised when a path resolves OUTSIDE every approved root.
    A SECURITY signal, deliberately NOT a subclass of the exceptions the
    file/deliverable layers swallow — it must propagate, not degrade to a
    soft 'could not read' string."""
    # carry the offending path + the roots that were checked for logging
    def __init__(self, path, roots): ...
```

```python
def normalize_roots(roots):
    """Canonicalize a list of approved root dirs to realpath'd absolute paths.
    - None -> []  (deny-all)
    - drops blanks; realpath()s each; de-dups; returns a list[str].
    Cheap; safe to call per-request."""
```

```python
def is_in_workspace(path, roots) -> bool:
    """True iff the realpath of `path` is strictly inside one of the
    realpath'd `roots`. False for an empty/None roots list (deny-all).
    Pure predicate — never raises for a normal 'outside' answer."""
```

```python
def assert_in_workspace(path, roots):
    """Raise WorkspaceViolation unless `path` is inside an approved root.
    Returns the realpath'd path on success (callers may use the canonical form).
    This is THE chokepoint. Call it BEFORE opening/reading/writing `path`."""
```

Implementation requirements for the resolve/compare core (get these exactly right — they are the security boundary):
- Resolve the candidate with `real = os.path.realpath(os.path.abspath(path))`. Using `realpath` (not just `abspath`) is mandatory: it collapses `..` AND resolves symlinks, so a symlink inside an approved root that points at `/etc/passwd` is rejected.
- Resolve each root with `os.path.realpath(os.path.abspath(root))` (do this once in `normalize_roots`).
- Containment test per root: compute `os.path.commonpath([real, root])` inside a `try/except ValueError` (commonpath raises `ValueError` when paths are on different drives on Windows, or mixing absolute/relative) and treat a `ValueError` as "not contained" (deny). A path is in-workspace iff `commonpath([real, root]) == root` for some root.
- IMPORTANT prefix-escape bug to avoid: do NOT use `real.startswith(root)`. `"/home/user/work-evil".startswith("/home/user/work")` is `True` but is an ESCAPE. `commonpath` is the correct primitive precisely because it is path-segment-aware. Add a code comment saying so.
- A root that equals the path exactly counts as inside (a directory itself is in its own workspace) — `commonpath([root, root]) == root` already gives this; just confirm.
- Empty/None `roots` (after normalization) -> deny-all: `is_in_workspace` returns `False`, `assert_in_workspace` raises. Add a comment: "Empty allowlist is intentional default-deny, not an accident; server mode opts out explicitly by skipping the call, never by passing a wildcard root."
- Do NOT special-case `/` or accept a wildcard sentinel — there is intentionally no "allow everything" value. The only way to be unrestricted is to not call the gate (server mode), which is an explicit decision at the call site.

### 2. Wire into `file_tools.parse_file`
- Add `from . import workspace` at the top of `file_tools.py`.
- Add a new keyword-only parameter to the signature: `def parse_file(path, *, roots=None):`. Default `roots=None` so every EXISTING caller (`app.py` server uploads, `base_agent`, tests) keeps working byte-for-byte with no allowlist enforcement — server mode is unrestricted by omission.
- When `roots` is not None (i.e. a caller is opting into the sandbox — the future local-daemon path-aware `/api/run`), call `workspace.assert_in_workspace(path, roots)` as the FIRST statement, BEFORE the `try/except` that wraps the readers. This placement is critical: `WorkspaceViolation` must escape `parse_file` to the caller, not be caught by the `except Exception` and turned into a benign `{"text": "[Could not read this file…]"}` dict. (Because `WorkspaceViolation` is NOT raised from inside the try block, the existing handler won't see it. Keep it that way. Do NOT widen the except to catch it.)
- When `roots is None`, do nothing — preserve today's behavior exactly.

### 3. Wire into `deliverables.run_tool`
- Add `from . import workspace` at the top of `deliverables.py`.
- Add a keyword-only parameter: `def run_tool(name, tool_input, out_dir, *, roots=None):`. Default `None` so `base_agent.py:89` and `tests/test_deliverables.py` keep passing unchanged (server mode unrestricted).
- Compute the final `path` exactly as today (`_unique` + `_safe_filename`), then — when `roots is not None` — call `workspace.assert_in_workspace(path, roots)` AFTER `path` is computed but BEFORE `builder(tool_input, path)` (i.e. before any write/`open`). Optionally also assert `out_dir` itself is in-workspace up front, before `os.makedirs(out_dir, exist_ok=True)`, so a malicious `out_dir` can't create directories outside the sandbox; if you do both, the per-file assert is still the load-bearing one.
- `run_tool`'s contract is "never raises — returns a result dict". For a `WorkspaceViolation`, do NOT swallow it into a generic `{"ok": False, "message": "Could not create…"}` that looks like a transient build error the model might retry around. Instead, catch `WorkspaceViolation` SPECIFICALLY (before/separately from the broad `except Exception`) and return `{"ok": False, "message": "...", "denied": True}` with a clear "outside the approved workspace" message — a distinct, security-flavored failure the runner can log/surface. Keep the existing broad `except Exception` for genuine build failures. (Rationale: a soft retry-able error and a hard security denial are different events; tests will assert the denial is distinguishable.)
- When `roots is None`, behavior is identical to today.

### 4. Optional config accessor (only if trivial; otherwise skip)
If WS1 has not yet landed a `LOCAL_MODE`/roots source, you do NOT need to invent the env plumbing here — `roots` is passed by the caller. But add a tiny, well-documented helper in `agent_team/workspace.py` for the future runner to use, following the `config._env`/`_TRUE` style:
```python
def roots_from_env():
    """Parse TANGENT_WORKSPACE_ROOTS (os.pathsep-separated) into a normalized
    allowlist. Unset/blank -> [] (deny-all). For the local daemon to populate
    from the folder(s) the user picked; the SERVER never sets this."""
```
Do not call this anywhere in server code paths. It exists so WS5/the runner can call `parse_file(path, roots=workspace.roots_from_env())`. If wiring an env var risks scope-creep or conflicts with WS1, omit `roots_from_env` and just ship the three core functions — the call sites taking `roots=...` is the deliverable.

## Invariants to respect (do not regress any of these)
- **China domestic mode**: this is pure stdlib filesystem code with zero LLM/provider/model coupling. Do not import `providers`, `config.effective_model`, `token_manager`, or anything network. Must work identically whether `TEAM_MODEL_PRESET=domestic` or not.
- **No model keys / no inference here**: WS2 touches only filesystem access. Do not add any auth, device-token, or gateway logic.
- **¥ cost-control untouched**: do not alter `platform_guard`, `token_manager`, `record_usage`, or any billing path. The chokepoint is orthogonal to cost.
- **Founder-gating untouched**: no new admin/customer-visible surface; no endpoints.
- **Files-encrypted-at-rest preserved**: `crypto.encrypt_file_in_place(path)` must still run exactly where it does in `run_tool` (after a successful build). The workspace check runs BEFORE the write; it does not touch crypto.
- **Honesty / never-fake**: not directly applicable (no deliverables faked), but keep the deliverables module's tone — the denial message must be truthful and specific ("outside the approved workspace"), never a misleading generic error.
- **Server-mode compatibility (CRITICAL)**: every existing caller (`app.py` uploads via `parse_file`, `base_agent` via `run_tool`, all current tests) passes NO `roots` and MUST be unaffected. Default-deny applies ONLY when a caller explicitly opts in by passing `roots=[...]`. Verify nothing in the current suite breaks.

## Acceptance criteria
1. `agent_team/workspace.py` exists with `assert_in_workspace`, `is_in_workspace`, `normalize_roots`, `WorkspaceViolation` (and optionally `roots_from_env`), stdlib-only.
2. `parse_file(path, *, roots=None)` and `run_tool(name, tool_input, out_dir, *, roots=None)` gained the keyword-only param; with `roots=None` they behave exactly as before.
3. The full existing test suite still passes: `.venv/bin/python -m pytest -q` (no regressions; the current pass count must hold).
4. New test file `tests/test_workspace.py` (mirror the header style of `tests/test_deliverables.py`) covering, at minimum:
   - **Deny-all default**: `is_in_workspace("/anything", [])` is `False`; `is_in_workspace(x, None)` is `False`; `assert_in_workspace(x, [])` raises `WorkspaceViolation`.
   - **In-root allow**: a file created under `tmp_path` with `roots=[str(tmp_path)]` passes; `assert_in_workspace` returns the realpath'd path.
   - **`..` traversal denied**: `assert_in_workspace(str(tmp_path / ".." / "secret"), [str(tmp_path)])` raises.
   - **Symlink escape denied** (the headline test): create `tmp_path/inside/link` as a symlink pointing to a file OUTSIDE `tmp_path` (e.g. another tmp dir or a sentinel under `tmp_path.parent`); with `roots=[str(tmp_path)]`, `assert_in_workspace(link_path, roots)` raises because `realpath` resolves the symlink target outside the root. (Use `os.symlink`; skip on platforms/CI where symlink creation is unavailable via `pytest.mark.skipif`/try-except.)
   - **Prefix-escape NOT allowed**: with `roots=[str(tmp_path / "work")]`, a sibling `tmp_path / "work-evil" / "f.txt"` is denied (guards against the `startswith` bug).
   - **`parse_file` enforcement**: `parse_file(outside_path, roots=[str(tmp_path)])` raises `WorkspaceViolation` (NOT a `{"text": "[Could not read…]"}` dict — assert the exception propagates); `parse_file(inside_path, roots=[str(tmp_path)])` returns a normal parsed dict; `parse_file(outside_path)` (no roots) still returns the normal dict (server-mode compat).
   - **`run_tool` enforcement**: calling `run_tool("create_excel_workbook", payload, out_dir=<dir outside roots>, roots=[str(tmp_path)])` returns `{"ok": False, "denied": True, ...}` and writes NO file outside the sandbox; the same call with `out_dir` inside `roots` succeeds and produces a real `.xlsx` (read it back via `crypto.read_plaintext` like the existing deliverables test). `run_tool(..., roots=None)` writes the file as today.
5. Run the targeted suites green: `.venv/bin/python -m pytest tests/test_workspace.py tests/test_deliverables.py -v`.

## Explicitly OUT of scope (do NOT do these here)
- The local daemon / tray / `runner/` supervisor, waitress boot, `LOCAL_MODE` flag plumbing (that is WS1).
- Device pairing, `tng-dev-` tokens, `devices`/`agent_catalog`/`agent_install`/`agent_run` tables, migration `0007`, and any `app.py` route (`/api/devices/*`, `/api/agents/catalog`, path-aware `/api/run`). NO new migration in this workstream — you are not touching `models.py` or `migrations/`.
- The RemoteRouter / `/v1` gateway auth extension and the local tool-execution loop (WS5).
- Changing the server-side `parse_file` call sites in `app.py` to pass roots — server uploads stay unrestricted; only the future daemon path opts in.
- Any UI/frontend change, native file pickers, watchdog, or "reveal in Finder".
- An "allow everything" wildcard root sentinel — there is intentionally none; unrestricted = don't call the gate.

## How to run
- Test runner: `/Users/brucetang/Company_Agent_Demo/.venv/bin/python -m pytest` (there is no `pyproject.toml`/`pytest.ini`; tests self-insert the project root into `sys.path`, so run from the repo root). Migrations head is `0006` (irrelevant to this WS since you add no table).
