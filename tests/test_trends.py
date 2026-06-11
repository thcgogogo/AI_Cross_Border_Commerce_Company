"""
test_trends.py — Acceptance tests for the deterministic trend & anomaly engine.

Run from the project root:

    .venv/bin/python -m pytest tests/test_trends.py -v

These exercise PURE math (no LLM, no network): given a seeded `metrics` table we
assert exact week-over-week deltas, consecutive rise/fall streaks, and the
anomaly flags (>2σ from the trailing mean, |WoW| > 25%, or a streak). Each test
runs against its OWN isolated SQLite file, so the real brain is never touched.
"""

import os
import sys

import pytest

# Make the project root importable no matter where pytest is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import db, metrics_store, trends  # noqa: E402
from agent_team.models import Metric  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """Point the company brain at a throwaway SQLite file for this one test."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


def _seed(cid, metric_name, values, labels=None, dimension=None, name_cn="销售额"):
    """Insert one Metric row per value, oldest first, under a fresh run.

    Labels default to consecutive weekly periods ("2026-W10", "2026-W11", …) so
    the engine's chronological ordering matches the order we pass `values` in.
    """
    rid = metrics_store.create_run(cid, f"seed {metric_name}")
    if labels is None:
        labels = [f"2026-W{10 + i}" for i in range(len(values))]
    with db.session_scope() as s:
        for label, value in zip(labels, values):
            s.add(Metric(
                company_id=cid, run_id=rid, period_label=label,
                metric_name=metric_name, metric_name_cn=name_cn,
                value=value, unit="CNY", dimension=dimension,
                source_file="seed.xlsx",
            ))
    return rid


# ---------------------------------------------------------------------------
def test_four_consecutive_falls_is_a_down_streak(fresh_db):
    """5 strictly-falling periods -> a 'down' streak of length 4, and the anomaly
    list carries a streak item whose Chinese sentence says '连续4'."""
    cid = metrics_store.get_or_create_company("streak@corp.com")
    _seed(cid, "total_sales", [500, 400, 300, 200, 100], name_cn="销售额")

    streak = trends.detect_streaks(cid, "total_sales")
    assert streak == {"direction": "down", "length": 4}

    anomalies = trends.detect_anomalies(cid)
    streak_items = [a for a in anomalies if a["kind"] == "streak"]
    assert len(streak_items) == 1
    assert "连续4" in streak_items[0]["human_cn"]


def test_single_spike_is_flagged_with_exact_wow(fresh_db):
    """A flat series followed by one +40% jump -> a 'spike' with wow_pct == 40.0."""
    cid = metrics_store.get_or_create_company("spike@corp.com")
    _seed(cid, "total_sales", [100, 100, 100, 100, 140])

    deltas = trends.compute_deltas(cid, "total_sales")
    assert deltas["latest"] == 140
    assert deltas["wow_pct"] == 40.0

    anomalies = trends.detect_anomalies(cid)
    spikes = [a for a in anomalies if a["kind"] == "spike"]
    assert len(spikes) == 1
    assert spikes[0]["wow_pct"] == 40.0
    assert spikes[0]["latest"] == 140


def test_single_period_history_no_anomaly_no_crash(fresh_db):
    """One lonely data point can't be judged: no anomalies, no streak, no crash."""
    cid = metrics_store.get_or_create_company("lonely@corp.com")
    _seed(cid, "total_sales", [100])

    assert trends.detect_anomalies(cid) == []
    assert trends.detect_streaks(cid, "total_sales") is None

    deltas = trends.compute_deltas(cid, "total_sales")
    assert deltas["latest"] == 100
    assert deltas["prev"] is None
    assert deltas["wow_pct"] is None
    assert deltas["yoy_pct"] is None


def test_division_by_zero_yields_none_and_is_skipped(fresh_db):
    """When the previous period is 0, WoW is undefined (None) and the latest value
    is NOT flagged as an anomaly (no false spike from a div-by-zero)."""
    cid = metrics_store.get_or_create_company("zerodiv@corp.com")
    _seed(cid, "total_sales", [100, 100, 100, 0, 100])

    deltas = trends.compute_deltas(cid, "total_sales")
    assert deltas["latest"] == 100
    assert deltas["prev"] == 0
    assert deltas["wow_pct"] is None  # division by zero -> None, never a crash

    assert trends.detect_anomalies(cid) == []  # skipped entirely
