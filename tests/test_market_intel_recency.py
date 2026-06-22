"""
test_market_intel_recency.py — the HEADLINE guarantee of 市场情报周报: freshness is
deterministic. The LLM never owns the clock or the recency filter — Python does.

Network-free: the search backend is the in-process 'mock' with injected results dated
across 2024→2026. These tests assert the engine, given a fixed 'today', will:
  - build queries with the CURRENT year/最新 and NEVER a hard-coded 2024;
  - DROP every result older than the window AND every undated result, BEFORE the writer;
  - carry the publish_date into every 【N】 source row;
  - build the 截止日期 table date-sorted and confined to the deadline window.
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import config, market_intel_engine as eng, market_intel_search  # noqa: E402

TODAY = datetime.date(2026, 6, 22)   # a Monday
WL = {"platforms": ["amazon"], "markets": ["US"], "category": "家居", "keywords": []}

# Distinct URLs; an official source so they also clear the 政策 hard-filter. Dates span the
# recency window (default 14d → window starts 2026-06-08).
_ITEMS = [
    {"title": "美国关税政策更新，报名截止 2026-07-10", "url": "https://www.customs.gov.cn/fresh",
     "snippet": "卖家需在 2026-07-10 前完成申报。", "publish_date": "2026-06-22",
     "source_name": "customs.gov.cn"},
    {"title": "海运运费回落", "url": "https://www.customs.gov.cn/inwindow",
     "snippet": "本周运费下降。", "publish_date": "2026-06-21", "source_name": "customs.gov.cn"},
    {"title": "两年前的旧闻", "url": "https://www.customs.gov.cn/stale",
     "snippet": "过期内容。", "publish_date": "2024-06-01", "source_name": "customs.gov.cn"},
    {"title": "无日期来源", "url": "https://www.customs.gov.cn/undated",
     "snippet": "没有发布日期。", "publish_date": None, "source_name": "customs.gov.cn"},
]


def _use_mock(monkeypatch):
    monkeypatch.setenv("MI_SEARCH_BACKEND", "mock")
    monkeypatch.setattr(config, "current_date_str", lambda: TODAY.isoformat())
    monkeypatch.setattr(market_intel_search, "_MOCK_RESULTS", lambda q: list(_ITEMS))


def test_queries_inject_current_year_never_2024(monkeypatch):
    monkeypatch.setattr(config, "current_date_str", lambda: TODAY.isoformat())
    queries = eng._expand_queries([WL], TODAY)
    joined = " ".join(q["query"] for q in queries)
    assert "2026" in joined                 # the current year is injected, in code
    assert "2024" not in joined             # never a hard-coded stale year
    assert any("最新" in q["query"] for q in queries)


def test_recency_gate_drops_stale_and_undated(monkeypatch):
    _use_mock(monkeypatch)
    pool = eng.collect_daily_intel([WL], today=TODAY)
    kept = [it for items in pool["items_by_section"].values() for it in items]
    urls = {it["url"] for it in kept}
    # only the two in-window items survive; stale (2024) + undated are gone.
    assert "https://www.customs.gov.cn/fresh" in urls
    assert "https://www.customs.gov.cn/inwindow" in urls
    assert "https://www.customs.gov.cn/stale" not in urls
    assert "https://www.customs.gov.cn/undated" not in urls
    window_start = TODAY - datetime.timedelta(days=config.mi_recency_days())
    assert all(datetime.date.fromisoformat(it["publish_date"]) >= window_start for it in kept)
    assert pool["dropped_count"] > 0


def test_publish_date_flows_into_sources_and_deadline_table(monkeypatch):
    _use_mock(monkeypatch)
    pool = eng.collect_daily_intel([WL], today=TODAY)
    p = eng.personalize_for_watchlist(pool, WL, "测试店铺", today=TODAY)
    # Every source row carries a publish + retrieved date (section-8 columns).
    assert p["source_rows"] and all(r["publish_date"] and r["retrieved_date"]
                                    for r in p["source_rows"])
    assert "链接" in p["sources_md"] and "发布日期" in p["sources_md"]
    # The 2026-07-10 deadline is within the 28-day window → in the date-sorted table.
    assert p["deadline_count"] >= 1
    assert "2026-07-10" in p["deadlines_md"]


def test_stale_item_never_reaches_the_writer(monkeypatch):
    """End-to-end of the principle: nothing dated 2024 can appear in what the writer sees."""
    _use_mock(monkeypatch)
    pool = eng.collect_daily_intel([WL], today=TODAY)
    p = eng.personalize_for_watchlist(pool, WL, "测试店铺", today=TODAY)
    assert "2024" not in p["facts_block"]
    assert "/stale" not in " ".join(r["url"] for r in p["source_rows"])
