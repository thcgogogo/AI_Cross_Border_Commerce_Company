# Tangent Agent — China channel (CN build)

The CN build is the SAME binary as the global build; it differs only in two SERVER/env
defaults (invariant: the client never chooses a model and never needs anthropic.com):

1. `__CLOUD_BASE_URL__` defaults to the **mainland, ICP-filed** gateway host (auth + `/v1`).
2. The cloud runs `TEAM_MODEL_PRESET=domestic`, so every model turn resolves to DeepSeek/
   Qwen server-side. The desktop sends no model id and needs no Anthropic key.

## Launch checklist (CN)

- [ ] **软著 (software copyright registration)** filed for the installer binary.
- [ ] The signed `.exe` + installer submitted to AV vendors for whitelisting, to avoid
      false-positive quarantine of the PyInstaller binary (a known China-channel pain point):
  - [ ] **360 安全卫士**
  - [ ] **火绒 (Huorong)**
  - [ ] **腾讯电脑管家 (Tencent)**
- [ ] The updater `endpoints` host is ICP-filed (it serves `latest.json` + bundles).
- [ ] Allow AV-whitelisting lead time before a public CN launch — start the filings early.

## Fallback (documented, NOT implemented here)

If the tray + loopback-CSRF + AV-false-positive surface proves too painful on the Windows
China channel, fall back to **Form Factor C** — a cloud-orchestrated thin outbound-WSS
bridge (the agent brain stays in the cloud; a tiny daemon answers read/write-file RPCs over
an outbound websocket). It ships a far smaller binary and sidesteps the loopback-server
attack surface, at the cost of per-tool-call cloud round-trips. This is the escape hatch,
not the v1 design.

## Manual smoke checklist (things tests can't cover cheaply)

- [ ] The signed `.dmg` / `.msi` double-clicks past Gatekeeper / SmartScreen.
- [ ] The app launches, drops a tray icon, and opens the SPA on `http://127.0.0.1:<port>`.
- [ ] The native folder picker returns paths; a local run produces a real `.xlsx`.
- [ ] "Download" → "Reveal in Finder/Explorer" opens the file's folder.
- [ ] Pairing stores the `tng-dev-` token in the OS keychain (not on disk); re-launch reuses it.
- [ ] An update `vN` → `vN+1` advertised in `latest.json` is accepted by the installed app.
