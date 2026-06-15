# Tangent Agent — auto-update signing

Tauri's updater accepts a new build only if it is signed with the **Ed25519 update key**
(separate from OS code-signing). Generate it once:

```
npx @tauri-apps/cli signer generate -w tangent-updater.key
```

This prints the **public** key (put it in `tauri.conf.json` → `plugins.updater.pubkey`)
and writes the **private** key to `tangent-updater.key`.

## ⚠️ PRIVATE KEY LOSS IS UNRECOVERABLE

If the Ed25519 **private** key is lost, **no future build can produce an update the
installed base will accept** — every existing user is stranded and must manually
re-download and reinstall. There is no recovery, no reset, no override.

**The moment the key is generated, back it up out-of-band (a sealed secrets vault), then:**

- [ ] Store `tangent-updater.key` + its password in the org password manager / sealed vault.
- [ ] Add a second offline backup (encrypted, in a separate location).
- [ ] Put the private key + password into CI secrets as `TAURI_SIGNING_PRIVATE_KEY` and
      `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` (used by the release workflow to sign bundles).
- [ ] Confirm the public key in `tauri.conf.json` matches before the FIRST public release —
      it cannot be changed for the installed base afterward without the same stranding.
- [ ] NEVER commit the private key. NEVER paste it into chat/logs.

## latest.json

CI generates a per-target update signature (`.sig`) for each bundle and assembles a
`latest.json` (`src-tauri/scripts/gen_latest_json.py`) listing version + per-target URL +
signature. It is published to the **ICP-filed** host configured in `endpoints`.
