"""
trends.py — Deterministic (non-LLM) trend & anomaly math over the company brain.

Phase 1 stores every numeric metric (see metrics_store / models.Metric). This
module reads that `metrics` table and computes EXACT trends — week-over-week and
year-over-year deltas, consecutive rise/fall streaks, and statistical anomalies —
so a report can say "已连续三周下滑，本周环比 -12.4%" with numbers the LLM cannot
hallucinate. The orchestrator injects the results into the Report Writer's prompt
as PRE-COMPUTED facts it must use verbatim.

No LLM calls. Pure Python + SQLAlchemy reads. Every public function returns plain
values (dicts / lists), never live ORM objects, so they're safe across threads.

Period ordering: metric rows carry a string `period_label` ("2026-W23", "2026-05",
…). We order chronologically by `period_start` when present, else by parsing the
label, else by insertion id — and "same period last year" is found by decrementing
the leading 4-digit year of the label (so "2026-W23" -> "2025-W23").
"""

import re
import statistics

from . import db
from .models import Metric

# How many trailing-period rises/falls make a "streak" worth reporting.
DEFAULT_MIN_STREAK = 3
# Anomaly thresholds (per the spec): >2σ from the trailing mean, or |WoW| > 25%.
SIGMA_THRESHOLD = 2.0
WOW_PCT_THRESHOLD = 25.0
# Need at least this many periods of history before we judge the latest value.
MIN_HISTORY_FOR_ANOMALY = 4


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_deltas(company_id, metric_name, dimension=None):
    """Latest period vs the previous period and vs the same period last year.

    Returns {"latest", "prev", "wow_pct", "yoy_pct"}. Percentages are rounded to
    1 decimal. wow_pct / yoy_pct are None when the comparison period is missing or
    its value is 0 (division by zero).
    """
    points = _ordered_series(company_id, metric_name, dimension)
    if not points:
        return {"latest": None, "prev": None, "wow_pct": None, "yoy_pct": None}

    latest = points[-1]
    prev = points[-2] if len(points) >= 2 else None
    wow = _pct_change(latest["value"], prev["value"]) if prev else None

    yoy = None
    yoy_label = _prev_year_label(latest["label"])
    if yoy_label:
        match = next((p for p in points if p["label"] == yoy_label), None)
        if match is not None:
            yoy = _pct_change(latest["value"], match["value"])

    return {
        "latest": latest["value"],
        "prev": prev["value"] if prev else None,
        "wow_pct": _round1(wow),
        "yoy_pct": _round1(yoy),
    }


def detect_streaks(company_id, metric_name, dimension=None, min_len=DEFAULT_MIN_STREAK):
    """Detect a run of consecutive rises/falls ending at the latest period.

    Returns {"direction": "up"|"down", "length": n} where `length` counts the
    consecutive same-direction steps (so 5 falling periods in a row -> length 4),
    or None if the latest move is flat or the run is shorter than `min_len`.
    """
    points = _ordered_series(company_id, metric_name, dimension)
    values = [p["value"] for p in points]
    return _streak_from_values(values, min_len=min_len)


def metric_series(company_id, metric_name, dimension=None, last_n=None):
    """Public, chart-ready view of one chronological metric series (redesign
    B6 — the 企业大脑 KPI sparklines): [{"label", "value"}], oldest first,
    optionally trimmed to the most recent `last_n` points. Same ordering and
    same one-point-per-period dedup as every other function here."""
    points = _ordered_series(company_id, metric_name, dimension)
    if last_n:
        points = points[-int(last_n):]
    return [{"label": p["label"], "value": p["value"]} for p in points]


def detect_anomalies(company_id):
    """Flag the latest value of every (metric_name, dimension) series with >=4
    periods of history when it deviates from the trailing mean by more than 2
    standard deviations, OR |WoW| > 25%, OR it caps a consecutive streak.

    Returns a list of dicts:
      {"metric_name_cn", "dimension", "latest", "wow_pct",
       "kind": "spike"|"drop"|"streak", "human_cn": one-line Chinese sentence}
    """
    if not company_id:
        return []

    with db.session_scope() as s:
        rows = (s.query(Metric)
                .filter(Metric.company_id == company_id)
                .order_by(Metric.id.asc())
                .all())
        # Group by (metric_name, dimension); within each, keep one point per
        # period_label (the latest write — highest id — wins).
        groups = {}
        for m in rows:
            if m.value is None:
                continue
            key = (m.metric_name, m.dimension)
            groups.setdefault(key, {})[m.period_label] = {
                "label": m.period_label, "value": m.value, "max_id": m.id,
                "start": m.period_start, "name_cn": m.metric_name_cn,
            }

        results = []
        for (metric_name, dimension), by_label in groups.items():
            points = sorted(by_label.values(),
                            key=lambda p: _period_rank(p["label"], p["start"], p["max_id"]))
            item = _anomaly_for_series(points, metric_name, dimension)
            if item is not None:
                results.append(item)
        return results


# ---------------------------------------------------------------------------
# Core computation (pure, testable on plain value lists)
# ---------------------------------------------------------------------------
def _anomaly_for_series(points, metric_name, dimension):
    if len(points) < MIN_HISTORY_FOR_ANOMALY:
        return None
    values = [p["value"] for p in points]
    latest, prev = values[-1], values[-2]
    wow = _pct_change(latest, prev)  # None if prev == 0 (division by zero)

    trailing = values[:-1]
    mean = statistics.fmean(trailing)
    std = statistics.pstdev(trailing) if len(trailing) >= 2 else 0.0

    is_sigma = std > 0 and abs(latest - mean) > SIGMA_THRESHOLD * std
    is_wow = wow is not None and abs(wow) > WOW_PCT_THRESHOLD
    streak = _streak_from_values(values, min_len=DEFAULT_MIN_STREAK)

    if not (is_sigma or is_wow or streak):
        return None

    name_cn = points[-1]["name_cn"] or metric_name
    wow_r = _round1(wow)
    prefix = _dim_prefix(dimension)

    if streak:
        kind = "streak"
        direction_cn = "下滑" if streak["direction"] == "down" else "上升"
        human = (f"{prefix}{name_cn}连续{streak['length']}周{direction_cn}"
                 f"，本周环比 {_fmt_pct(wow_r)}")
    else:
        up = (wow > 0) if wow is not None else (latest > mean)
        kind = "spike" if up else "drop"
        word = "异常上升" if up else "异常下滑"
        human = f"{prefix}{name_cn}本周{word}，环比 {_fmt_pct(wow_r)}"

    return {
        "metric_name_cn": name_cn,
        "dimension": dimension,
        "latest": latest,
        "wow_pct": wow_r,
        "kind": kind,
        "human_cn": human,
    }


def _streak_from_values(values, min_len=DEFAULT_MIN_STREAK):
    if len(values) < 2:
        return None
    last_dir = _step_dir(values[-2], values[-1])
    if last_dir == 0:
        return None  # latest move is flat
    length = 1
    i = len(values) - 2
    while i >= 1 and _step_dir(values[i - 1], values[i]) == last_dir:
        length += 1
        i -= 1
    if length < min_len:
        return None
    return {"direction": "down" if last_dir < 0 else "up", "length": length}


def _step_dir(a, b):
    if b > a:
        return 1
    if b < a:
        return -1
    return 0


def _pct_change(curr, prev):
    """Percent change, or None on missing inputs / division by zero."""
    if curr is None or prev is None:
        return None
    if prev == 0:
        return None
    return (curr - prev) / prev * 100.0


def _round1(x):
    """Every percentage shown to users is rounded to 1 decimal."""
    return None if x is None else round(float(x), 1)


def _fmt_pct(x):
    return "N/A" if x is None else f"{x:.1f}%"


def _dim_prefix(dimension):
    """Render a dimension as a human prefix: "region=华东" -> "华东"."""
    if not dimension:
        return ""
    val = dimension.split("=", 1)[1] if "=" in dimension else dimension
    return val.strip()


# ---------------------------------------------------------------------------
# Period ordering + label math
# ---------------------------------------------------------------------------
def _ordered_series(company_id, metric_name, dimension=None):
    """Load one chronological series for (company, metric, dimension). Returns a
    list of plain dicts {label, value, max_id, start, name_cn}, oldest first."""
    if not company_id or not metric_name:
        return []
    with db.session_scope() as s:
        q = s.query(Metric).filter(Metric.company_id == company_id,
                                   Metric.metric_name == metric_name)
        q = q.filter(Metric.dimension.is_(None)) if dimension is None \
            else q.filter(Metric.dimension == dimension)
        rows = q.order_by(Metric.id.asc()).all()
        by_label = {}
        for m in rows:
            if m.value is None:
                continue
            by_label[m.period_label] = {
                "label": m.period_label, "value": m.value, "max_id": m.id,
                "start": m.period_start, "name_cn": m.metric_name_cn,
            }
        return sorted(by_label.values(),
                      key=lambda p: _period_rank(p["label"], p["start"], p["max_id"]))


def _period_rank(label, period_start, max_id):
    """A sortable key putting periods in chronological order. Prefers an explicit
    start date, else parses the label, else falls back to insertion id."""
    if period_start is not None:
        return (period_start.year, period_start.month, period_start.day, max_id)
    parsed = _parse_label(label)
    if parsed is not None:
        return (parsed[0], parsed[1], parsed[2], max_id)
    # Unparseable: rely on insertion order (sorts before any dated period).
    return (-1, -1, -1, max_id)


def _parse_label(label):
    """Parse common period labels into a comparable (year, sub, sub2) tuple."""
    if not label:
        return None
    s = str(label).strip()
    m = re.match(r"^(\d{4})-W(\d{1,2})$", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), 0)
    m = re.match(r"^(\d{4})-Q(\d)$", s, re.I)
    if m:
        return (int(m.group(1)), int(m.group(2)), 0)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), 0)
    m = re.match(r"^(\d{4})$", s)
    if m:
        return (int(m.group(1)), 0, 0)
    return None


def _prev_year_label(label):
    """The label one year earlier: decrement the leading 4-digit year, keep the
    rest verbatim. "2026-W23" -> "2025-W23"; "2026-05" -> "2025-05"."""
    if not label:
        return None
    m = re.match(r"^(\d{4})(.*)$", str(label).strip())
    if not m:
        return None
    return f"{int(m.group(1)) - 1}{m.group(2)}"
