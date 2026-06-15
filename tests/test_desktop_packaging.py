"""
test_desktop_packaging.py — WS11: desktop build/shell/distribution CONTRACTS.

Hermetic (no Tauri/Rust/PyInstaller, no network): asserts the packaging FILES carry the
right invariants — sidecar dep exclusions + version floors, the PyInstaller spec hooks,
no keys baked, the Tauri config security/capabilities, the LOCAL_MODE route gating +
shared-secret seam (already provided by the runner), the cloud-base substitution, the
updater key-loss warning, and the CI honesty gate.
"""
import json
import os
import re
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402

DESKTOP_REQ = os.path.join(ROOT, "requirements-desktop.txt")
SPEC = os.path.join(ROOT, "build", "agentcore.spec")
TAURI_CONF = os.path.join(ROOT, "src-tauri", "tauri.conf.json")
CAPS = os.path.join(ROOT, "src-tauri", "capabilities", "default.json")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "desktop-release.yml")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _parse_reqs(text):
    """{lowercased name: constraint-string} from a requirements file (comments stripped)."""
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?(.*)$", line)
        if m:
            out[m.group(1).lower()] = (m.group(3) or "").strip()
    return out


# ---------------------------------------------------------------------------
# 1. requirements-desktop.txt — exclusions, keeps, version floors
# ---------------------------------------------------------------------------
def test_desktop_requirements_exclusions_and_keeps():
    desktop = _parse_reqs(_read(DESKTOP_REQ))
    for excluded in ("psycopg2-binary", "psycopg2", "gunicorn", "redis", "alembic", "sentry-sdk"):
        assert excluded not in desktop, f"{excluded} must be excluded from the sidecar"
    for kept in ("pandas", "openpyxl", "pypdf", "python-docx", "python-pptx",
                 "reportlab", "cryptography", "keyring", "waitress"):
        assert kept in desktop, f"{kept} must be in the sidecar"


def test_desktop_version_floors_match_main():
    main = _parse_reqs(_read(os.path.join(ROOT, "requirements.txt")))
    desktop = _parse_reqs(_read(DESKTOP_REQ))
    for name, constraint in desktop.items():
        if name in main:
            assert constraint == main[name], f"{name}: desktop {constraint!r} != main {main[name]!r}"


# ---------------------------------------------------------------------------
# 2 & 3. PyInstaller spec sanity + no keys baked
# ---------------------------------------------------------------------------
def test_pyinstaller_spec_contracts():
    spec = _read(SPEC)
    assert "TANGENT_TARGET_TRIPLE" in spec and "tangent-agentcore" in spec    # triple naming
    assert "collect_data_files(\"reportlab\")" in spec                        # CJK font data
    assert "9360" in spec                                                     # msvcp140 workaround note
    for excl in ("psycopg2", "gunicorn", "alembic", "redis", "sentry_sdk"):
        assert excl in spec                                                   # in excludes=[...]


def test_no_provider_keys_baked():
    spec, req = _read(SPEC), _read(DESKTOP_REQ)
    assert "sk-ant-" not in spec and "sk-ant-" not in req
    # No .env file is bundled as a PyInstaller data entry (a quoted path ending in .env).
    assert re.search(r'["\'][^"\']*\.env["\']', spec) is None
    # anthropic may be present (imported at load) but ONLY with no key bundled.
    assert "ANTHROPIC_API_KEY" not in spec


# ---------------------------------------------------------------------------
# 4. tauri.conf.json + capabilities invariants
# ---------------------------------------------------------------------------
def test_tauri_conf_invariants():
    conf = json.loads(_read(TAURI_CONF))
    assert conf["bundle"]["externalBin"] == ["binaries/tangent-agentcore"]
    upd = conf["plugins"]["updater"]
    # The PUBLIC update key is set; no private-key field is embedded in the config.
    assert upd["pubkey"] and "PRIVATE" not in upd["pubkey"].upper()
    assert not any("private" in k.lower() for k in upd)
    csp = conf["app"]["security"]["csp"]
    assert "127.0.0.1" in csp and "unsafe-eval" not in csp


def test_capabilities_are_minimal():
    perms = json.dumps(json.loads(_read(CAPS))["permissions"])
    assert "dialog:allow-open" in perms
    assert "opener:allow-reveal-item-in-dir" in perms
    assert "127.0.0.1" in perms
    assert "shell:" not in perms and "fs:" not in perms                       # no broad fs/shell


# ---------------------------------------------------------------------------
# 5 & 6. LOCAL_MODE route gating + shared-secret seam (provided by the runner)
# ---------------------------------------------------------------------------
@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(app, "LOCAL_MODE", True)
    monkeypatch.setattr(app, "LOCAL_CSRF_TOKEN", "secret123")
    return app.app.test_client()


def test_local_mode_route_gating(local):
    assert local.get("/").status_code == 200                                  # SPA reachable
    assert local.get("/api/orders").status_code == 404                        # payment hidden
    assert local.get("/api/scheduled-jobs").status_code == 404                # scheduler hidden
    assert local.get("/api/models").status_code in (403, 404)                 # founder machinery locked
    # The run + gateway routes still exist (not 404) under LOCAL_MODE.
    assert local.post("/api/run/local", json={}).status_code != 404


def test_shared_secret_enforced(local):
    # A mutating /api/* without the secret header → rejected.
    assert local.post("/api/run/local", json={"goal": "x", "paths": []}).status_code == 403
    # A cross-origin Origin → rejected even with the header.
    bad = local.post("/api/run/local", json={"goal": "x"},
                     headers={"X-Tangent-Local-Token": "secret123", "Origin": "http://evil.example"})
    assert bad.status_code == 403
    # With the correct secret + same-origin → passes the guard (reaches auth → 401).
    ok = local.post("/api/run/local", json={"goal": "x", "paths": []},
                    headers={"X-Tangent-Local-Token": "secret123"})
    assert ok.status_code != 403


# ---------------------------------------------------------------------------
# 7. Cloud-base substitution + no machinery in the served HTML
# ---------------------------------------------------------------------------
def test_cloud_base_substitution(local, monkeypatch):
    monkeypatch.setenv("TANGENT_CLOUD_URL", "https://api.tangent.example")
    html = local.get("/").get_data(as_text=True)
    assert "window.__CLOUD_BASE_URL__ = \"https://api.tangent.example\"" in html
    assert "__TANGENT_CLOUD_BASE__" not in html                               # placeholder filled
    assert "claude-opus" not in html and "input_per_mtok" not in html         # no model/¥ leak


# ---------------------------------------------------------------------------
# 8. Updater key-loss doc
# ---------------------------------------------------------------------------
def test_updater_doc_warns_key_loss():
    doc = _read(os.path.join(ROOT, "src-tauri", "UPDATER.md")).upper()
    assert "UNRECOVERABLE" in doc and "BACK" in doc


# ---------------------------------------------------------------------------
# 9. CI honesty gate
# ---------------------------------------------------------------------------
def test_ci_matrix_and_signing_gate():
    wf = yaml.safe_load(_read(WORKFLOW))
    triples = {inc["triple"] for inc in wf["jobs"]["build"]["strategy"]["matrix"]["include"]}
    assert triples == {"aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-pc-windows-msvc"}
    text = _read(WORKFLOW)
    assert "Require signing secrets on a release tag" in text                 # fails without secrets
    assert "-unsigned" in text and "dev_build" in text                        # labelled dev path
