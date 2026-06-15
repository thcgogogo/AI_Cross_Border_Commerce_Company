"""
workspace.py — the default-deny filesystem chokepoint for locally-deployed agents.

Every local-file access (the path-aware run intake, and later the read/list/write
LOCAL tools and the deliverable builders) must pass its path through
``assert_in_workspace()`` BEFORE opening anything. The rule: a path is allowed only
if, after fully resolving symlinks (``os.path.realpath``), it lives inside one of the
user-approved root directories. With no approved roots, EVERYTHING is rejected
(default-deny) — so a prompt-injected spreadsheet can never coax an agent into
reading ``~/.ssh``.

This module has ZERO Flask/app dependency, so it is trivially unit-testable and safe
to import from anywhere. Roots are process-global (the daemon is single-tenant: one
logged-in user on their own machine) and mutated only by the supervisor / the
watched-folders UI via ``set_roots`` / ``add_root``.
"""
import os
import threading

_lock = threading.Lock()
_roots = []  # list[str]: absolute, realpath-normalized approved directories


class WorkspaceError(Exception):
    """Raised when a path is outside every approved root (or otherwise unsafe).

    A SECURITY signal — deliberately NOT a subclass of the exceptions the file/
    deliverable layers swallow, so it propagates rather than degrading to a soft
    'could not read' string."""


# Alias for call sites that read as a security check (parse_file / run_tool wiring).
WorkspaceViolation = WorkspaceError


def _norm(path):
    """Fully resolve a path: expand ~, make absolute, resolve every symlink.

    realpath is applied LAST so a symlink that sits inside an approved root but
    points outside it resolves to its real (outside) location and is then rejected
    by the containment check."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def set_roots(paths):
    """Replace the approved-root set (each entry expanded/abspath'd/realpath'd).
    Returns the normalized roots now in effect."""
    norm = []
    for p in paths or []:
        if not p:
            continue
        r = _norm(p)
        if r not in norm:
            norm.append(r)
    with _lock:
        global _roots
        _roots = norm
        return list(_roots)


def add_root(path):
    """Approve one more root directory at runtime (the watched-folders UI calls this).
    Returns the full approved-root set."""
    if not path:
        return approved_roots()
    r = _norm(path)
    with _lock:
        if r not in _roots:
            _roots.append(r)
        return list(_roots)


def approved_roots():
    with _lock:
        return list(_roots)


def is_within(real_path, root):
    """True iff ``real_path`` equals ``root`` or is nested under it. Uses commonpath
    so there is no ``/a/b`` vs ``/a/bc`` prefix-string bug (NEVER use startswith here);
    returns False across drives (Windows) or on malformed input."""
    try:
        return os.path.commonpath([root, real_path]) == root
    except ValueError:
        return False


def normalize_roots(roots):
    """Canonicalize approved roots to realpath'd absolute paths (None → []), dropping
    blanks and de-duping. Cheap; safe to call per-request."""
    out = []
    for r in roots or []:
        if not r:
            continue
        nr = _norm(r)
        if nr not in out:
            out.append(nr)
    return out


def is_in_workspace(path, roots):
    """Pure predicate: True iff the realpath of ``path`` is inside one of the realpath'd
    ``roots``. Empty/None roots → False (default-deny). Never raises for a normal
    'outside' answer, and does NOT require the path to exist (containment only — so it
    is usable for a not-yet-created write target)."""
    norm = normalize_roots(roots)
    if not norm or not path:
        return False
    real = _norm(path)
    return any(is_within(real, root) for root in norm)


def roots_from_env():
    """Parse TANGENT_WORKSPACE_ROOTS (os.pathsep-separated) into a normalized allowlist.
    Unset/blank → [] (deny-all). For the local daemon to populate from the folders the
    user picked; the SERVER never sets this."""
    raw = os.environ.get("TANGENT_WORKSPACE_ROOTS", "") or ""
    return normalize_roots([p for p in raw.split(os.pathsep) if p.strip()])


def assert_in_workspace(path, roots=None, *, require_file=True, require_exists=True):
    """Resolve ``path`` and confirm it lives inside an approved root.

    Returns the resolved real path on success; raises ``WorkspaceError`` otherwise.

    Default-deny: with no approved roots, every path is rejected. The realpath is
    resolved BEFORE the containment check, so a symlink inside an approved root that
    points outside it is rejected. ``require_file`` rejects an EXISTING directory when
    a file is wanted; ``require_exists`` (default True) keeps the read/run-intake
    behavior — set it False for a not-yet-created WRITE target (its containment is
    still enforced, since realpath resolves the existing prefix)."""
    if not path:
        raise WorkspaceError("空路径。")
    roots = approved_roots() if roots is None else [_norm(r) for r in roots if r]
    if not roots:
        raise WorkspaceError("没有已授权的工作目录（默认拒绝所有路径）。")
    real = _norm(path)
    exists = os.path.exists(real)
    if require_exists and not exists:
        raise WorkspaceError(f"路径不存在：{path}")
    if require_file and exists and not os.path.isfile(real):
        raise WorkspaceError(f"不是文件：{path}")
    for root in roots:
        if is_within(real, root):
            return real
    raise WorkspaceError(f"路径不在授权目录内：{path}")
