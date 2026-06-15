# -*- mode: python ; coding: utf-8 -*-
# build/agentcore.spec — PyInstaller --onedir spec for the Tangent Agent sidecar.
#
# Produces the frozen Python core the Tauri shell launches. Entry point is the runner
# supervisor (agent_team/runner/__main__.py), which boots app.py under waitress in
# LOCAL_MODE and prints TANGENT_DAEMON_PORT=<n> on stdout.
#
# Output naming — Tauri's `externalBin` appends the target triple, so the binary MUST be
# named `tangent-agentcore-<triple>` (e.g. tangent-agentcore-aarch64-apple-darwin,
# tangent-agentcore-x86_64-pc-windows-msvc.exe). Pass the triple via TANGENT_TARGET_TRIPLE
# in CI; the same spec builds all three. PyInstaller CANNOT cross-compile — one native
# runner per target (see .github/workflows/desktop-release.yml).
#
# Build:  TANGENT_TARGET_TRIPLE=<triple> pyinstaller build/agentcore.spec
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPECPATH)))  # deterministic; no abs machine paths
_TRIPLE = os.environ.get("TANGENT_TARGET_TRIPLE", "").strip()
_NAME = "tangent-agentcore" + (f"-{_TRIPLE}" if _TRIPLE else "")

# --- Data files bundled into the sidecar -----------------------------------------------
datas = []
# The SPA the daemon serves over loopback, the industry packs, and the bundled CJK font.
datas += [(os.path.join(REPO_ROOT, "frontend"), "frontend")]
datas += [(os.path.join(REPO_ROOT, "templates_industry"), "templates_industry")]
datas += [(os.path.join(REPO_ROOT, "assets"), "assets")]
# reportlab ships CJK CMap/CID font data PyInstaller misses by default — without these the
# branded report PDF (agent_team/pdf_report.py) renders □□□ for Chinese. Collect them.
datas += collect_data_files("reportlab")
# openpyxl / python-pptx carry XML templates as package data.
datas += collect_data_files("openpyxl")
datas += collect_data_files("pptx")

# --- Hidden imports / submodules -------------------------------------------------------
hiddenimports = []
hiddenimports += collect_submodules("pandas")
# keyring resolves its OS backend lazily — pin the per-platform ones so the frozen build
# can store the device token in the real keychain.
hiddenimports += ["keyring.backends.macOS", "keyring.backends.Windows",
                  "keyring.backends.SecretService"]

# --- Windows numpy/msvcp140 workaround (pyinstaller#9360) -------------------------------
# numpy >= 2 (pulled in by pandas) needs the MSVC runtime DLLs collected, or `import
# pandas` fails on a clean Windows machine with a missing-DLL error. PyInstaller's bundled
# numpy hook does NOT yet collect msvcp140.dll. Re-check when the upstream hook lands
# (https://github.com/pyinstaller/pyinstaller/issues/9360) and drop this block.
binaries = []
if os.name == "nt":
    import ctypes.util
    for _dll in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        _found = ctypes.util.find_library(_dll)
        if _found:
            binaries.append((_found, "."))

# --- Server-only modules excluded so they're never pulled transitively -----------------
excludes = ["psycopg2", "psycopg2-binary", "gunicorn", "alembic", "redis", "sentry_sdk"]

block_cipher = None

a = Analysis(
    [os.path.join(REPO_ROOT, "agent_team", "runner", "__main__.py")],
    pathex=[REPO_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --onedir (NOT --onefile: onefile re-extracts ~180MB per launch and trips AV).
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=_NAME,
          console=True, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, name="tangent-agentcore")
