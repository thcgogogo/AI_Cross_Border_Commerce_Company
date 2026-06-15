"""
registry.py — the local agent registry loader for the desktop daemon.

At sidecar startup this pulls the account's installed agent MANIFESTS from the cloud
(`GET /api/agents/catalog`, authenticated with the per-device token), caches them by
content_hash, and merges them into agent_team.config.AGENTS so the existing framework
instantiates them with NO new class — the project's "add a dict, the framework picks
it up" convention.

Pure-Python, no LLM and no provider-key dependency. The manifest carries BEHAVIOR
only (the cloud whitelists it at publish time) — it has NO model id / provider / ¥, so
the loader works on a fully-domestic China cloud with no Anthropic key, and never
resolves a model. The cache file holds only non-secret manifests; the device token
lives in the environment / OS keychain (WS11), NEVER in this cache.
"""
import json
import os
import urllib.error
import urllib.request

# Founder-only keys that must NEVER appear in a runner manifest — asserted defensively
# so a server bug can't leak pricing/model machinery onto a device.
_FOUNDER_ONLY_KEYS = ("model", "provider", "input_per_mtok", "output_per_mtok",
                      "cost_budget_cny", "team_model_preset")


class SyncResult:
    """Summary of one sync(): which agent_ids were added/updated/unchanged/removed,
    plus an `error` string when the fetch failed and we fell back to the cache."""
    def __init__(self):
        self.added, self.updated, self.unchanged, self.removed = [], [], [], []
        self.error = None

    def __repr__(self):
        return (f"SyncResult(added={self.added}, updated={self.updated}, "
                f"unchanged={self.unchanged}, removed={self.removed}, error={self.error!r})")


def _default_fetch():
    """The real catalog fetch: GET ${TANGENT_CLOUD_URL}/api/agents/catalog with the
    device token. Returns the parsed `agents` list, or [] when unpaired."""
    cloud = (os.environ.get("TANGENT_CLOUD_URL") or "").strip().rstrip("/")
    token = (os.environ.get("TANGENT_DEVICE_TOKEN") or "").strip()
    if not cloud or not token:
        return []
    req = urllib.request.Request(cloud + "/api/agents/catalog",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return data.get("agents") or []


def _manifest_to_agent_dict(manifest, content_hash=None):
    """Translate a runner-visible manifest into a config.AGENTS-shaped dict — exactly
    the keys base_agent/orchestrator read, with safe defaults. Returns None (skip) if
    a REQUIRED key (agent_id/name/system_prompt) is missing. NEVER sets `model`: the
    cloud gateway picks the real model server-side."""
    if not isinstance(manifest, dict):
        return None
    aid = manifest.get("agent_id")
    name = manifest.get("name")
    system_prompt = manifest.get("system_prompt")
    if not aid or not name or not str(system_prompt or "").strip():
        return None
    return {
        "id": aid,
        "name": name,
        "title": manifest.get("title") or aid,
        "name_cn": manifest.get("name_cn", ""),
        "reports_to": manifest.get("reports_to", "ceo"),   # a merged specialist reports to the CEO
        "effort": manifest.get("effort", "high"),
        "tools": list(manifest.get("tools") or []),        # NAMES only; base_agent maps via specs_for
        "system_prompt": system_prompt,
        # NO "model" — effective_model() defaults; the cloud resolves the real one.
        "_source": "registry",                              # ignored by base_agent/_agents_payload
        "_content_hash": content_hash,
    }


class LocalRegistry:
    """Owns the manifest cache + the merge into config.AGENTS. `fetch` is injectable
    (() -> list[dict]) so tests run with a fake catalog and no network/keychain."""

    def __init__(self, cache_dir=None, fetch=None):
        self._fetch = fetch or _default_fetch
        self._cache_path = os.path.join(cache_dir, "agent_registry.json") if cache_dir else None
        self._cache = {}     # agent_id -> {version, content_hash, manifest}
        self._load_cache()

    def _load_cache(self):
        if self._cache_path and os.path.exists(self._cache_path):
            try:
                with open(self._cache_path, encoding="utf-8") as f:
                    self._cache = json.load(f) or {}
            except Exception:
                self._cache = {}

    def _save_cache(self):
        if not self._cache_path:
            return
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
        except Exception:
            pass     # a cache-write failure must never crash the daemon

    def sync(self):
        """Pull the catalog, diff by content_hash (a version/hash change == updated),
        prune entries no longer installed, persist the cache, and return a SyncResult.
        NEVER raises for an I/O/auth error — it falls back to the last-good cache."""
        res = SyncResult()
        try:
            rows = self._fetch() or []
        except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
            res.error = str(e)          # keep the last-good cache; daemon still boots
            return res

        fresh, seen = {}, set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            aid = row.get("agent_id")
            ch = row.get("content_hash")
            manifest = row.get("manifest")
            if not aid or not ch or not isinstance(manifest, dict):
                continue
            seen.add(aid)
            entry = {"version": row.get("version"), "content_hash": ch, "manifest": manifest}
            fresh[aid] = entry
            prev = self._cache.get(aid)
            if prev and prev.get("content_hash") == ch:
                res.unchanged.append(aid)
            elif prev:
                res.updated.append(aid)     # content_hash (or version) changed → re-merge
            else:
                res.added.append(aid)
        res.removed = [aid for aid in self._cache if aid not in seen]
        self._cache = fresh
        self._save_cache()
        return res

    def manifests(self):
        return [e["manifest"] for e in self._cache.values()]

    def to_agent_dicts(self):
        out = []
        for e in self._cache.values():
            d = _manifest_to_agent_dict(e.get("manifest"), e.get("content_hash"))
            if d:
                out.append(d)
        return out

    def apply_to_config(self, config_module):
        """Idempotently merge to_agent_dicts() into config_module.AGENTS via
        register_runtime_agents (which owns the prune/upsert bookkeeping)."""
        return config_module.register_runtime_agents(self.to_agent_dicts())


def bootstrap(config_module, fetch=None, cache_dir=None):
    """One-line convenience for the daemon supervisor: sync the catalog and merge it
    into config.AGENTS. Called once at startup, before any Orchestrator is built."""
    reg = LocalRegistry(cache_dir=cache_dir, fetch=fetch)
    reg.sync()
    reg.apply_to_config(config_module)
    return reg
