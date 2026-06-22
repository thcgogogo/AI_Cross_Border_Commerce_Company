"""
test_market_intel.py — 市场情报周报: the engine's whitelist/personalize/anchor rules and
the /api/market-intel/* HTTP surface (auth, validation, quota, customer-safe result).

Hermetic (no network); the search backend is the in-process 'mock' and the writer model
is faked but still bills the real per-account path. Mirrors the 对账/AR route tests.
"""
import datetime
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import (config, db, market_intel_engine as eng,  # noqa: E402
                        market_intel_orchestrator, market_intel_search,
                        metrics_store as ms)
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import (QUOTA_RUN_KINDS, RUN_KIND_MARKET_INTEL,  # noqa: E402
                               RUN_KIND_MI_COLLECT)

TODAY = datetime.date(2026, 6, 22)
WL = {"platforms": ["amazon"], "markets": ["US"], "category": "家居", "keywords": []}


# ---------------------------------------------------------------------------
# Fixtures (mirror test_ar_routes)
# ---------------------------------------------------------------------------
@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "brain.db"))
    yield


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(ms, "REPORTS_DIR", str(tmp_path / "reports"))
    yield


def _store(tmp_path, **accounts):
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    for email, password in accounts.items():
        store.create_customer([], name=email.split("@")[0], starting_balance=100.0,
                              email=email, password=password)
    app.CUSTOMERS = store
    return store


def _login(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


class _FakeRouter:
    """Returns the five narrative sections as JSON (the writer contract). No anchors, so
    the anchor-integrity audit stays clean; the engine's deterministic tables carry 【N】."""
    REPLY = ('{"highlights":"今日要点一", "policy":"无重大变动", "cost":"成本归纳", '
             '"demand":"需求归纳", "competition":"竞品动态归纳"}')

    def __init__(self, token_manager=None, cost_listener=None, **k):
        self.cost_listener = cost_listener
        self.token_manager = token_manager

    def complete(self, **kw):
        if self.cost_listener:
            self.cost_listener(config.HAIKU, 40_000, 6_000)
        return self.REPLY


def _pro(monkeypatch):
    """Make the logged-in account a 企业版 subscriber (the daily report's plan feature)."""
    monkeypatch.setattr(ms, "effective_plan", lambda *a, **k: ("pro", None, False))


def _seed_pool(monkeypatch, items):
    monkeypatch.setenv("MI_SEARCH_BACKEND", "mock")
    monkeypatch.setattr(config, "current_date_str", lambda: TODAY.isoformat())
    monkeypatch.setattr(market_intel_search, "_MOCK_RESULTS", lambda q: list(items))
    pool = eng.collect_daily_intel([WL], today=TODAY)
    ms.upsert_mi_pool(pool["period_label"], pool)
    return pool


# Dates within the daily 2-day recency window (TODAY=06-22 → window starts 06-20).
_FRESH = [{"title": "关税更新 截止 2026-07-10", "url": "https://www.customs.gov.cn/a",
           "snippet": "申报。", "publish_date": "2026-06-22", "source_name": "customs.gov.cn"},
          {"title": "运费回落", "url": "https://www.customs.gov.cn/b", "snippet": "下降。",
           "publish_date": "2026-06-21", "source_name": "customs.gov.cn"}]


# ---------------------------------------------------------------------------
# Engine rules
# ---------------------------------------------------------------------------
def test_market_intel_is_pro_feature_not_quota():
    # 跨境市场情报日报 is a 企业版 subscription feature, NOT per-report-quota-metered.
    assert RUN_KIND_MARKET_INTEL not in QUOTA_RUN_KINDS
    assert RUN_KIND_MI_COLLECT not in QUOTA_RUN_KINDS
    assert config.plan_has("pro", "market_intel") is True
    assert config.plan_has("free", "market_intel") is False
    assert config.plan_has("basic", "market_intel") is False


def test_local_mode_blocks_market_intel():
    assert "/api/market-intel" in app._LOCAL_MODE_BLOCKED_PREFIXES


def test_policy_section_hard_filtered_to_official(monkeypatch):
    monkeypatch.setenv("MI_SEARCH_BACKEND", "mock")
    monkeypatch.setattr(config, "current_date_str", lambda: TODAY.isoformat())
    official = {"title": "海关公告", "url": "https://www.customs.gov.cn/x", "snippet": "官方。",
                "publish_date": "2026-06-20", "source_name": "customs.gov.cn"}
    blog = {"title": "某博客谈政策", "url": "https://randomblog.com/y", "snippet": "非官方。",
            "publish_date": "2026-06-20", "source_name": "randomblog.com"}
    monkeypatch.setattr(market_intel_search, "_MOCK_RESULTS", lambda q: [official, blog])
    pool = eng.collect_daily_intel([WL], today=TODAY)
    policy_urls = {it["url"] for it in pool["items_by_section"]["policy"]}
    other_urls = {it["url"] for s in ("cost", "demand")
                  for it in pool["items_by_section"][s]}
    assert "https://www.customs.gov.cn/x" in policy_urls      # official → kept in 政策
    assert "https://randomblog.com/y" not in policy_urls      # non-official → dropped from 政策
    assert "https://randomblog.com/y" in other_urls           # but kept where not hard-filtered


def test_deadline_extraction_rolls_over_year_boundary():
    # A bare "1月15日" parsed on Dec 30 must mean NEXT January, not this (past) January.
    dec30 = datetime.date(2026, 12, 30)
    window_end = dec30 + datetime.timedelta(days=28)
    d = eng._extract_deadline("报名截止 1月15日 前完成", dec30, window_end)
    assert d == datetime.date(2027, 1, 15)
    # A bare date already in the past this year and beyond next-year's window → not picked.
    assert eng._extract_deadline("活动 6月1日", dec30, window_end) is None


def test_daily_pool_is_keyed_by_date(monkeypatch):
    monkeypatch.setenv("MI_SEARCH_BACKEND", "mock")
    monkeypatch.setattr(config, "current_date_str", lambda: TODAY.isoformat())
    monkeypatch.setattr(market_intel_search, "_MOCK_RESULTS", lambda q: list(_FRESH))
    pool = eng.collect_daily_intel([WL], today=TODAY)
    assert pool["period_label"] == "2026-06-22"          # daily key, not an ISO week
    assert pool["recency_days"] == 2


def test_empty_day_says_no_news(monkeypatch):
    monkeypatch.setenv("MI_SEARCH_BACKEND", "mock")
    monkeypatch.setattr(config, "current_date_str", lambda: TODAY.isoformat())
    monkeypatch.setattr(market_intel_search, "_MOCK_RESULTS", lambda q: [])   # nothing today
    pool = eng.collect_daily_intel([WL], today=TODAY)
    p = eng.personalize_for_watchlist(pool, WL, "店铺", today=TODAY)
    assert p["no_news"] is True and p["kept_count"] == 0
    assert "今日暂无重大市场情报" in p["det_sections"]["highlights"]


def test_section7_competition_and_price_note(monkeypatch):
    monkeypatch.setenv("MI_SEARCH_BACKEND", "mock")
    monkeypatch.setattr(config, "current_date_str", lambda: TODAY.isoformat())
    monkeypatch.setattr(market_intel_search, "_MOCK_RESULTS", lambda q: list(_FRESH))
    wl = dict(WL, competitors=["Anker"])
    # a 竞品动态 query is generated per competitor
    assert any("Anker" in q["query"] for q in eng._expand_queries([wl], TODAY))
    pool = eng.collect_daily_intel([wl], today=TODAY)
    p = eng.personalize_for_watchlist(pool, wl, "店铺", today=TODAY)
    assert "竞品价格：待接入价格数据源" in p["det_sections"]["competition"]   # deferred note


def test_watchlist_keywords_drive_queries_and_scope():
    """B5 regression: a customer's keywords must actually drive searches + scoping
    (previously collected & saved but ignored)."""
    wl = dict(WL, keywords=["air fryer"])
    qs = eng._expand_queries([wl], TODAY)
    assert any("air fryer" in q["query"] and q.get("q_keyword") == "air fryer" for q in qs)
    # An empty keywords field generates NO keyword query (adds no searches / COGS).
    assert not any(q.get("q_keyword")
                   for q in eng._expand_queries([dict(WL, keywords=[])], TODAY))
    # _matches_watchlist scopes a keyword-tagged item to the matching watchlist only.
    item = {"q_keyword": "air fryer"}
    assert eng._matches_watchlist(item, [], [], "", [], ["air fryer"]) is True
    assert eng._matches_watchlist(item, [], [], "", [], ["robot vacuum"]) is False


def test_personalize_filters_to_watchlist():
    pool = {"items_by_section": {"cost": [
        {"title": "amazon item", "url": "u1", "snippet": "", "publish_date": "2026-06-20",
         "source_name": "s", "section": "cost", "topic_cn": "运费",
         "q_platform": "amazon", "q_market": "US", "q_category": "家居"},
        {"title": "temu item", "url": "u2", "snippet": "", "publish_date": "2026-06-20",
         "source_name": "s", "section": "cost", "topic_cn": "运费",
         "q_platform": "temu", "q_market": "US", "q_category": "家居"},
    ], "policy": [], "demand": []}}
    p = eng.personalize_for_watchlist(pool, WL, "店铺", today=TODAY)   # amazon-only watchlist
    claims = " ".join(r["claim_cn"] for r in p["source_rows"])
    assert "amazon item" in claims and "temu item" not in claims      # temu filtered out
    assert p["kept_count"] == 1


def test_orphan_anchor_audit_flags_invented_anchor(monkeypatch):
    monkeypatch.setenv("MI_SEARCH_BACKEND", "mock")
    monkeypatch.setattr(config, "current_date_str", lambda: TODAY.isoformat())
    monkeypatch.setattr(market_intel_search, "_MOCK_RESULTS", lambda q: list(_FRESH))
    pool = eng.collect_daily_intel([WL], today=TODAY)

    class _Mem:
        def __init__(self): self.job_id = "j"; self._f = ""
        def set_status(self, *a): pass
        def log(self, *a, **k): pass
        def set_company_id(self, v): pass
        def set_run_id(self, v): pass
        def set_sources(self, *a): pass
        def set_final_report(self, t): self._f = t
        def set_error(self, e): pass
        def snapshot(self): return {"final_report": self._f, "sources": [], "sources_status": "ok"}

    class _OrphanRouter(_FakeRouter):
        REPLY = '{"highlights":"要点【999】", "policy":"无", "cost":"无", "demand":"无"}'

    mem = _Mem()
    market_intel_orchestrator.MarketIntelOrchestrator(
        mem, pool, WL, company_name="店铺", llm=_OrphanRouter(),
        company_id=None, run_id=None).run()
    assert "请人工确认" in mem._f      # the invented 【999】 is flagged, never silently trusted


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
def test_run_requires_login(fresh_db, tmp_path):
    _store(tmp_path)
    assert app.app.test_client().post("/api/market-intel/run").status_code == 401


def test_run_requires_pro_plan(fresh_db, tmp_storage, tmp_path):
    """A non-企业版 account can't generate — the daily report is a plan feature (402)."""
    store = _store(tmp_path, **{"free@corp.com": "pw"})   # default plan = free
    assert _login(store, "free@corp.com").post("/api/market-intel/run").status_code == 402


def test_run_without_watchlist_is_400(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _pro(monkeypatch)
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    r = _login(store, "a@corp.com").post("/api/market-intel/run")
    assert r.status_code == 400


def test_run_without_pool_is_409(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _pro(monkeypatch)
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    c = _login(store, "a@corp.com")
    c.post("/api/market-intel/watchlist", json={"platforms": ["amazon"], "markets": ["US"],
                                                "category": "家居"})
    assert c.post("/api/market-intel/run").status_code == 409


def test_markets_spa_shell_is_served(fresh_db, tmp_storage, tmp_path):
    """A hard load / refresh / shared deep link of /markets must serve the SPA shell
    (not 404). Logged-out hits redirect to login."""
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    c = _login(store, "a@corp.com")
    assert c.get("/markets").status_code == 200
    assert c.get("/markets/5").status_code == 200
    assert app.app.test_client().get("/markets").status_code in (301, 302)


def test_watchlist_roundtrip(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    c = _login(store, "a@corp.com")
    c.post("/api/market-intel/watchlist",
           json={"platforms": ["amazon", "temu"], "markets": ["US"], "category": "家居",
                 "keywords": ["air fryer"]})
    wl = c.get("/api/market-intel/watchlist").get_json()["watchlist"]
    assert wl["platforms"] == ["amazon", "temu"] and wl["category"] == "家居"


def _wait_done(client, job_id, tries=80):
    snap = {}
    for _ in range(tries):
        snap = client.get(f"/api/status/{job_id}").get_json()
        if snap.get("status") in ("done", "error"):
            return snap
        time.sleep(0.05)
    return snap


def test_full_market_intel_run(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _pro(monkeypatch)
    monkeypatch.setattr(market_intel_orchestrator, "ModelRouter", _FakeRouter)
    store = _store(tmp_path, **{"boss@corp.com": "pw"})
    c = _login(store, "boss@corp.com")
    c.post("/api/market-intel/watchlist",
           json={"platforms": ["amazon"], "markets": ["US"], "category": "家居"})
    _seed_pool(monkeypatch, _FRESH)   # one shared collection exists for today

    r = c.post("/api/market-intel/run")
    assert r.status_code == 200
    job_id, run_id = r.get_json()["job_id"], r.get_json()["run_id"]
    snap = _wait_done(c, job_id)
    assert snap["status"] == "done"
    # Customer-safe activity (no model names leaked).
    assert all("claude" not in (a.get("message") or "").lower()
               for a in snap.get("activity", []))

    res = c.get(f"/api/market-intel/result/{run_id}").get_json()
    md = res["final_report"]
    for header in ("抬头", "今日要点", "截止日期", "政策与合规", "成本与利润",
                   "需求与机会", "竞争态势", "数据来源与核验"):
        assert header in md
    assert "竞品价格：待接入价格数据源" in md          # 竞品价格 deferred note present
    assert res["period_label"] and len(ms.list_mi_reports(
        ms.get_or_create_company("boss@corp.com"))) == 1


def test_result_ownership_blocks_other_account(fresh_db, tmp_storage, tmp_path):
    """User B must NOT be able to read User A's market-intel report."""
    from agent_team.models import RUN_KIND_MARKET_INTEL
    store = _store(tmp_path, **{"a@corp.com": "pw", "b@corp.com": "pw"})
    cid_a = ms.get_or_create_company("a@corp.com", company_name="A 店铺")
    run_id = ms.create_run(cid_a, "市场情报周报", kind=RUN_KIND_MARKET_INTEL)
    ms.save_mi_report(cid_a, run_id, {"period_label": "2026-W25", "report_markdown": "# A 的报告"},
                      account_id="x", period_label="2026-W25")
    # owner can read it; a different account gets 403 (ownership enforced).
    assert _login(store, "a@corp.com").get(f"/api/market-intel/result/{run_id}").status_code == 200
    assert _login(store, "b@corp.com").get(f"/api/market-intel/result/{run_id}").status_code == 403


# ---------------------------------------------------------------------------
# 送达设置 (market-intel's OWN delivery subpage) + the channels-bug fix
# ---------------------------------------------------------------------------
def test_markets_settings_spa_shell_is_served(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    assert _login(store, "a@corp.com").get("/markets/settings").status_code == 200


def test_delivery_defaults_to_email(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    d = _login(store, "a@corp.com").get("/api/market-intel/delivery").get_json()["delivery"]
    assert d["channels"] == ["email"] and d["paused"] is False


def test_delivery_save_requires_pro(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"free@corp.com": "pw"})
    r = _login(store, "free@corp.com").post("/api/market-intel/delivery",
                                            json={"channels": ["email", "wechat"]})
    assert r.status_code == 402


def test_delivery_save_and_read_and_pause(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _pro(monkeypatch)
    store = _store(tmp_path, **{"boss@corp.com": "pw"})
    c = _login(store, "boss@corp.com")
    c.post("/api/market-intel/watchlist", json={"platforms": ["amazon"], "markets": ["US"],
                                                "category": "家居"})
    r = c.post("/api/market-intel/delivery", json={
        "channels": ["email", "wechat"],
        "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
        "paused": True})
    assert r.status_code == 200
    d = c.get("/api/market-intel/delivery").get_json()["delivery"]
    assert set(d["channels"]) == {"email", "wechat"}
    assert d["wechat_webhook"].startswith("https://qyapi.weixin.qq.com")
    assert d["paused"] is True
    # pausing must NOT wipe the watchlist's list fields (re-save full row)
    wl = c.get("/api/market-intel/watchlist").get_json()["watchlist"]
    assert wl["platforms"] == ["amazon"] and wl["active"] is False


def test_delivery_rejects_bad_webhook(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _pro(monkeypatch)
    store = _store(tmp_path, **{"boss@corp.com": "pw"})
    r = _login(store, "boss@corp.com").post("/api/market-intel/delivery",
                                            json={"wechat_webhook": "https://evil.example.com/x"})
    assert r.status_code == 400


def test_deliver_reads_mi_delivery_channels(fresh_db, tmp_storage, tmp_path, monkeypatch):
    """The daily-delivery bug fix: channels come from mi_delivery_channels, not always email."""
    cid = ms.get_or_create_company("boss@corp.com", company_name="店铺")
    ms.merge_profile(cid, {"mi_delivery_channels": ["email", "wechat"],
                           "wechat_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=z"})
    captured = {}
    monkeypatch.setattr(app.delivery, "deliver_report", lambda **kw: captured.update(kw) or [])

    class _Mem:
        job_id = "j"
        def snapshot(self): return {"final_report": "# r"}

    app._deliver_market_intel(cid, 7, "店铺", _Mem())
    assert set(captured["channels"]) == {"email", "wechat"}
    # with NO webhook, 企业微信 is dropped (would silently fail otherwise)
    ms.merge_profile(cid, {"wechat_webhook": ""})
    captured.clear()
    app._deliver_market_intel(cid, 7, "店铺", _Mem())
    assert captured["channels"] == ["email"]
