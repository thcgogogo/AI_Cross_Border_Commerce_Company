"""
industry_packs.py — Vertical template packs (Moat 2).

A "pack" is a per-industry bundle of CONTENT (not code) living on disk under
`templates_industry/<key>/pack.yaml`. It tailors a run to a vertical:

  - required_kpis        the KPIs a report in this industry should cover
  - benchmarks           conservative industry reference ranges (行业经验值)
  - report_skeleton_cn   the mandatory section order for the Report Writer
  - analyst_extra_prompt  extra guidance appended to the Data Analyst
  - writer_extra_prompt   extra guidance appended to the Report Writer

Two design rules from the spec:

  1. HOT-EDITABLE — `load_pack()` re-reads the YAML from disk on EVERY call (no
     caching), so a founder can iterate on pack content like content, not code:
     edit the file, start the next run, see the change. No app restart.

  2. FAIL LOUDLY AT APP START — `validate_all_packs()` is called once at boot and
     raises PackError (naming the file and the missing key) if any pack on disk is
     malformed, so a broken pack can never silently degrade a run.

The company's industry (from the Phase 3 企业大脑 profile) selects a pack
automatically; a per-run dropdown can override it. `resolve_key()` maps either a
Chinese profile label (e.g. "跨境电商") or a raw pack key (e.g. "cross_border_
ecommerce") to a pack key, falling back to "generic" for anything unknown.
"""

import os

import yaml

# templates_industry/ lives at the project root (one level above this package).
PACKS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "templates_industry"))

# The fallback pack — neutral skeleton, no industry-specific content.
GENERIC_KEY = "generic"

# Every pack.yaml MUST define these keys (validated loudly at app start).
REQUIRED_KEYS = (
    "display_name_cn",
    "required_kpis",
    "benchmarks",
    "report_skeleton_cn",
    "analyst_extra_prompt",
    "writer_extra_prompt",
)
# Of those, these three must be YAML lists.
_LIST_KEYS = ("required_kpis", "benchmarks", "report_skeleton_cn")

# The Phase 3 profile stores a Chinese industry LABEL; map the ones we ship packs
# for to their pack key. Anything not here resolves to GENERIC_KEY.
INDUSTRY_LABEL_TO_KEY = {
    "跨境电商": "cross_border_ecommerce",
    "贸易": "trading_company",
    "贸易公司": "trading_company",
}

# VERBATIM (Phase 4 spec) — the instruction benchmarks are injected under. Do not
# edit: industry reference ranges must ALWAYS be labelled as such, never as the
# company's own data.
BENCHMARK_INSTRUCTION = (
    "Compare actuals to these reference ranges where applicable, and ALWAYS label "
    "them 行业经验值，仅供参考 — never present them as the company's own data."
)

# Headers the injected blocks lead with (so each block is unmistakable in a prompt).
PACK_KPI_HEADER = "=== 本行业关键指标 (REQUIRED KPIs FOR THIS INDUSTRY) ==="
BENCHMARK_HEADER = "=== 行业基准 (INDUSTRY BENCHMARKS) ==="
SKELETON_HEADER = (
    "=== 报告结构 (MANDATORY REPORT SECTION ORDER — use these exact section "
    "titles, in this order) ==="
)


class PackError(Exception):
    """A pack is missing, unreadable, or malformed. Raised loudly at app start."""


# ---------------------------------------------------------------------------
# Paths & resolution
# ---------------------------------------------------------------------------
def _pack_dir(key):
    return os.path.join(PACKS_DIR, key)


def _pack_path(key):
    return os.path.join(_pack_dir(key), "pack.yaml")


def list_pack_keys():
    """Every subfolder of templates_industry/ that contains a pack.yaml, sorted."""
    if not os.path.isdir(PACKS_DIR):
        return []
    keys = []
    for name in sorted(os.listdir(PACKS_DIR)):
        if os.path.isfile(_pack_path(name)):
            keys.append(name)
    return keys


def resolve_key(industry_value):
    """Resolve an industry value to a pack key, falling back to GENERIC_KEY.

    Accepts (in priority order): an exact pack key that exists on disk, a known
    Chinese profile label (INDUSTRY_LABEL_TO_KEY), or anything else -> generic.
    """
    v = (industry_value or "").strip()
    if not v:
        return GENERIC_KEY
    if os.path.isfile(_pack_path(v)):       # already a real pack key
        return v
    if v in INDUSTRY_LABEL_TO_KEY:          # a Chinese profile label
        return INDUSTRY_LABEL_TO_KEY[v]
    return GENERIC_KEY


# ---------------------------------------------------------------------------
# Loading & validation
# ---------------------------------------------------------------------------
def _read_yaml(path):
    """Parse a pack.yaml into a dict, or raise PackError naming the file."""
    if not os.path.isfile(path):
        raise PackError(f"Industry pack not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise PackError(f"{path}: invalid YAML — {e}")
    if not isinstance(data, dict):
        raise PackError(
            f"{path}: a pack must be a YAML mapping, got {type(data).__name__}.")
    return data


def _validate(data, path):
    """Raise PackError (naming the file and every offending key) if malformed."""
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise PackError(
            f"{path} is missing required key(s): {', '.join(missing)}.")
    bad_lists = [k for k in _LIST_KEYS if not isinstance(data.get(k), list)]
    if bad_lists:
        raise PackError(
            f"{path}: key(s) must be a list: {', '.join(bad_lists)}.")
    return data


def load_pack(industry_value):
    """Read + validate the pack for an industry value, FRESH from disk every call
    (hot-editable — there is intentionally no caching). Unknown/empty values
    resolve to the generic pack. Raises PackError if the resolved pack file is
    missing or malformed."""
    key = resolve_key(industry_value)
    path = _pack_path(key)
    data = _validate(_read_yaml(path), path)
    data["_key"] = key  # remember which pack this is, for logging/UI
    return data


def validate_all_packs():
    """Validate EVERY pack on disk. Call once at app start: raises PackError
    (naming the file and the missing/invalid key) if any pack is malformed, so the
    app refuses to start on a broken pack. Also requires the generic fallback."""
    keys = list_pack_keys()
    if GENERIC_KEY not in keys:
        raise PackError(
            f"Required fallback pack '{GENERIC_KEY}' not found under {PACKS_DIR}.")
    for key in keys:
        path = _pack_path(key)
        _validate(_read_yaml(path), path)


def available_industries():
    """[{key, display_name_cn}] for the task-form dropdown. Best-effort: skips/
    degrades gracefully on a malformed pack so the UI never crashes (the loud gate
    is validate_all_packs at boot)."""
    out = []
    for key in list_pack_keys():
        try:
            data = _read_yaml(_pack_path(key))
            label = (data.get("display_name_cn") or "").strip() or key
        except PackError:
            label = key
        out.append({"key": key, "display_name_cn": label})
    return out


# ---------------------------------------------------------------------------
# Prompt-block builders — turn a loaded pack into text appended to agent prompts.
# Each returns "" when there's nothing to inject, so callers can skip cleanly.
# ---------------------------------------------------------------------------
def build_required_kpi_block(pack):
    """The industry's required KPIs, merged into the Data Analyst's KPI mapping."""
    lines = []
    for k in (pack.get("required_kpis") or []):
        if not isinstance(k, dict):
            continue
        cn = (k.get("name_cn") or "").strip()
        en = (k.get("name") or "").strip()
        unit = (k.get("unit") or "").strip()
        label = cn or en
        if not label:
            continue
        bits = []
        if en and cn:
            bits.append(en)
        if unit:
            bits.append(f"单位 {unit}")
        suffix = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"- {label}{suffix}")
    if not lines:
        return ""
    return PACK_KPI_HEADER + "\n" + "\n".join(lines)


def build_benchmark_block(pack):
    """The industry benchmarks, under the verbatim 'label them 行业经验值' rule."""
    lines = []
    for b in (pack.get("benchmarks") or []):
        if not isinstance(b, dict):
            continue
        metric = (b.get("metric") or "").strip()
        rng = (b.get("range_text_cn") or "").strip()
        note = (b.get("source_note") or "").strip()
        if not metric and not rng:
            continue
        note_part = f"（{note}）" if note else ""
        lines.append(f"- {metric}: {rng}{note_part}")
    if not lines:
        return ""
    return BENCHMARK_HEADER + "\n" + BENCHMARK_INSTRUCTION + "\n" + "\n".join(lines)


def build_skeleton_block(pack):
    """The mandatory, ordered report section titles for the Report Writer."""
    items = [str(s).strip() for s in (pack.get("report_skeleton_cn") or []) if str(s).strip()]
    if not items:
        return ""
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(items))
    return SKELETON_HEADER + "\n" + numbered


def build_analyst_block(pack):
    """Everything appended to the Data Analyst's prompt for this pack: required
    KPIs (merged into the KPI instructions) + benchmarks + analyst_extra_prompt."""
    if not pack:
        return ""
    parts = [build_required_kpi_block(pack), build_benchmark_block(pack)]
    extra = (pack.get("analyst_extra_prompt") or "").strip()
    if extra:
        parts.append(extra)
    return "\n\n".join(p for p in parts if p)


def build_writer_block(pack):
    """Everything appended to the Report Writer's prompt for this pack: the
    mandatory section order + benchmarks + writer_extra_prompt."""
    if not pack:
        return ""
    parts = [build_skeleton_block(pack), build_benchmark_block(pack)]
    extra = (pack.get("writer_extra_prompt") or "").strip()
    if extra:
        parts.append(extra)
    return "\n\n".join(p for p in parts if p)
