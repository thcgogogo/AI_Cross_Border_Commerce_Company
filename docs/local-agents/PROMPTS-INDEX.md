# Execution prompts — locally-deployable agents

Each file is a **self-contained prompt** for a coding agent. Hand them out in dependency order (lowest phase first). A coding agent has repo access but **no memory of the planning conversation**, so each prompt restates the context it needs.

| WS | Phase | Workstream | Depends on |
|----|-------|-----------|------------|
| [WS01](execution-prompts/WS01_local-runner-supervisor-tray-path-based-run-inta.md) | 0 | Local runner supervisor + tray + path-based run intake | — |
| [WS02](execution-prompts/WS02_filesystem-allowlist-chokepoint.md) | 2 | Filesystem allowlist chokepoint | WS1 |
| [WS03](execution-prompts/WS03_device-token-model-store-migration.md) | 1 | Device-token model, store, migration | — |
| [WS04](execution-prompts/WS04_pairing-endpoints-current-device-v1-device-auth.md) | 1 | Pairing endpoints + _current_device + /v1 device auth | WS3 |
| [WS05](execution-prompts/WS05_remoterouter-local-tool-execution-loop.md) | 2 | RemoteRouter + local tool-execution loop | WS1, WS4 |
| [WS06](execution-prompts/WS06_flagship-excel-agent-read-file-list-dir-local-to.md) | 2 | Flagship Excel agent: read_file/list_dir LOCAL tools + drive_excel in-place editor | WS2, WS5 |
| [WS07](execution-prompts/WS07_agent-catalog-tables-publish-install-catalog-end.md) | 3 | Agent catalog: tables, publish/install/catalog endpoints, manifest split | WS3 |
| [WS08](execution-prompts/WS08_local-agent-registry-loader.md) | 3 | Local agent registry loader | WS7, WS1 |
| [WS09](execution-prompts/WS09_run-telemetry-sync-shared-run-lifecycle.md) | 3 | Run telemetry sync (shared run lifecycle) | WS3, WS4 |
| [WS10](execution-prompts/WS10_remove-report-delivery-pricing-from-the-run-path.md) | 3 | Remove report-delivery pricing from the run path | — |
| [WS11](execution-prompts/WS11_tauri-shell-pyinstaller-sidecar-signing-auto-upd.md) | 4 | Tauri shell, PyInstaller sidecar, signing, auto-update, CI | WS1, WS4, WS6 |
| [WS12](execution-prompts/WS12_watched-folder-automation-offline-local-model-mo.md) | 4 | Watched-folder automation + offline Local-Model mode (optional) | WS1, WS5 |
