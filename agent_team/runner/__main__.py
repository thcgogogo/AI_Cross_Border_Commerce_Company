"""
agent_team.runner.__main__ — `python -m agent_team.runner` and the PyInstaller entry.

This is the entry point the desktop sidecar (build/agentcore.spec) freezes and the Tauri
shell (src-tauri) spawns. It just runs the Supervisor, which:
  - reads TANGENT_LOCAL_SHARED_SECRET from the env (the per-launch secret the shell
    generated) and uses it as the loopback CSRF token,
  - picks a free 127.0.0.1 port, boots the existing app.py under waitress in LOCAL_MODE,
  - prints `TANGENT_DAEMON_PORT=<n>` on stdout so the shell learns the port,
  - drops a tray icon (or runs headless if no display).
No provider keys live here — inference is cloud-proxied via the /v1 gateway (RemoteRouter).
"""
from . import main

if __name__ == "__main__":
    raise SystemExit(main())
