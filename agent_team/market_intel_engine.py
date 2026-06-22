"""
market_intel_engine.py — the DETERMINISTIC core of 市场情报周报. NO LLM lives here.

Same principle as trends.py ("trends are deterministic; the LLM never does arithmetic"):
**the LLM never owns the clock or the recency filter — this module does, in pure Python.**

Two phases:

  collect_weekly_intel(watchlists, *, today)   — PHASE A, shared, run ONCE per week:
      build queries (with the CURRENT year/month injected — never a hard-coded year) →
      search → GATE every result by publish_date in code (drop stale + undated) →
      whitelist (政策与合规 hard-filtered to official sources) → dedup → bucket by section.
      Returns a JSON-serializable pool reused by every customer that week (the COGS split).

  personalize_for_watchlist(pool, watchlist, company_name, *, today)  — PHASE B, per customer:
      filter the pool to this watchlist → assign 【N】 anchors → build the masthead, the
      date-sorted 截止日期 table, the section-8 数据来源 table, the facts_block + source_rows.
      Returns everything the single-LLM writer needs (plus the anchor_set for the audit).

Recency invariant: query year, the publish_date filter, the deadline-table window + sort are
ALL computed here; the writer only words prose around the dated, 【N】-anchored items it is given.
"""
import datetime
import re

from . import config, market_intel_search

# Section keys that are SEARCHED (the rest are framing/derived in Python).
_SEARCHED_SECTIONS = ("policy", "cost", "demand", "competition")
_SECTION_TITLES = dict(config.MI_SECTIONS)
# A standing note for 竞争态势: 竞品价格 needs a pricing data source (deferred); only
# 竞品动态 (news) is covered today.
_COMPETITION_PRICE_NOTE = "竞品价格：待接入价格数据源（本期不覆盖，仅汇总竞品动态）。"


# ---------------------------------------------------------------------------
# Dates / labels
# ---------------------------------------------------------------------------
def today_date():
    """Today as a datetime.date in the deployment timezone (config.current_date_str)."""
    try:
        return datetime.date.fromisoformat(config.current_date_str())
    except Exception:
        return datetime.date.today()


def coverage_window(today):
    """The DAILY coverage window: the recency window the report covers, ending today."""
    return today - datetime.timedelta(days=config.mi_recency_days()), today


def pool_period_label(today):
    """The DATE key for the daily pool (one collection per day, reused across customers)."""
    return today.isoformat()


# ---------------------------------------------------------------------------
# Watchlist helpers
# ---------------------------------------------------------------------------
def _as_list(v):
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    if v is None:
        return []
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _wl_fields(watchlist):
    return (_as_list(watchlist.get("platforms")), _as_list(watchlist.get("markets")),
            (watchlist.get("category") or "").strip(),
            _as_list(watchlist.get("competitors")), _as_list(watchlist.get("keywords")))


# ---------------------------------------------------------------------------
# PHASE A — shared weekly collection
# ---------------------------------------------------------------------------
def _expand_queries(watchlists, today):
    """The fixed query plan × the deduped union of watchlist (platform, market, category,
    competitor, keyword). Each concrete query is built with the CURRENT {year}/{month};
    identical query strings across customers collapse so each topic is searched ONCE per
    day. A {competitor}/{keyword} template only fires when the watchlist actually lists
    competitors/keywords, so an empty field adds no searches (and no COGS)."""
    year, month = today.year, today.month
    seen = {}
    for wl in watchlists:
        platforms, markets, category, competitors, keywords = _wl_fields(wl)
        for entry in config.MI_QUERY_PLAN:
            tmpl = entry["query"]
            uses_p, uses_m, uses_c = ("{platform}" in tmpl, "{market}" in tmpl,
                                      "{category}" in tmpl)
            uses_comp, uses_kw = ("{competitor}" in tmpl, "{keyword}" in tmpl)
            for p in (platforms or [""]) if uses_p else [""]:
                for m in (markets or [""]) if uses_m else [""]:
                    for comp in (competitors or [""]) if uses_comp else [""]:
                        for kw in (keywords or [""]) if uses_kw else [""]:
                            # a competitor/keyword query with none configured is meaningless.
                            if (uses_comp and not comp) or (uses_kw and not kw):
                                continue
                            q = tmpl.format(year=year, month=month,
                                            platform=p if uses_p else "",
                                            market=m if uses_m else "",
                                            category=category if uses_c else "",
                                            competitor=comp if uses_comp else "",
                                            keyword=kw if uses_kw else "")
                            q = " ".join(q.split())  # collapse empty-placeholder spaces
                            if q and q not in seen:
                                seen[q] = {"query": q, "section": entry["section"],
                                           "topic_cn": entry["topic_cn"],
                                           "q_platform": p if uses_p else "",
                                           "q_market": m if uses_m else "",
                                           "q_category": category if uses_c else "",
                                           "q_competitor": comp if uses_comp else "",
                                           "q_keyword": kw if uses_kw else ""}
    return list(seen.values())


def _is_whitelisted(item, sources):
    hay = (item.get("url", "") + " " + item.get("source_name", "")).lower()
    return any(s in hay for s in sources)


def collect_daily_intel(watchlists, *, today=None, log=None):
    """PHASE A (once per day). Returns a JSON-serializable pool dict:
        {period_label, collected_at, query_plan_version, recency_days,
         items_by_section: {section: [item, ...]}, query_count, kept_count, dropped_count}
    where each item = {title, url, snippet, publish_date (ISO str), source_name, section,
    topic_cn, q_platform, q_market, q_category, q_competitor}. NO anchors yet (personalize
    assigns them)."""
    today = today or today_date()
    window_start = today - datetime.timedelta(days=config.mi_recency_days())
    whitelist = config.mi_source_whitelist()
    official = config.mi_official_sources()

    queries = _expand_queries(watchlists, today)
    by_section = {s: [] for s in _SEARCHED_SECTIONS}
    seen_urls = set()
    kept = dropped = 0

    for q in queries:
        results = market_intel_search.search(q["query"], start_date=window_start)
        for r in results:
            pub = r.get("publish_date")
            # --- THE RECENCY GATE (Python owns it): drop undated + stale, before the writer.
            if not isinstance(pub, datetime.date) or pub < window_start:
                dropped += 1
                continue
            url = r.get("url", "")
            if not url or url in seen_urls:
                continue
            # --- Source whitelist: 政策与合规 HARD-filtered to official; others prefer it.
            if q["section"] == "policy" and not _is_whitelisted(r, official):
                dropped += 1
                continue
            seen_urls.add(url)
            by_section[q["section"]].append({
                "title": r.get("title", ""), "url": url,
                "snippet": r.get("snippet", ""), "publish_date": pub.isoformat(),
                "source_name": r.get("source_name", ""), "section": q["section"],
                "topic_cn": q["topic_cn"], "q_platform": q["q_platform"],
                "q_market": q["q_market"], "q_category": q["q_category"],
                "q_competitor": q.get("q_competitor", ""),
                "q_keyword": q.get("q_keyword", ""),
                "whitelisted": _is_whitelisted(r, whitelist)})
            kept += 1

    # Within each section, newest first; whitelisted ahead of the rest at equal dates.
    for s in by_section:
        by_section[s].sort(key=lambda it: (it["publish_date"], it["whitelisted"]),
                           reverse=True)
    if log:
        log(f"市场情报采集：{len(queries)} 条检索，保留 {kept} 条（已按发布日期过滤掉 {dropped} 条）。")
    return {"period_label": pool_period_label(today), "collected_at": today.isoformat(),
            "query_plan_version": config.MI_QUERY_PLAN_VERSION,
            "recency_days": config.mi_recency_days(),
            "items_by_section": by_section, "query_count": len(queries),
            "kept_count": kept, "dropped_count": dropped}


# ---------------------------------------------------------------------------
# PHASE B — per-customer personalization (deterministic)
# ---------------------------------------------------------------------------
def _matches_watchlist(item, platforms, markets, category, competitors, keywords):
    """An item is in scope if its query context matches the watchlist (a blank context
    field = topic-agnostic, applies to everyone)."""
    if item.get("q_platform") and platforms and item["q_platform"] not in platforms:
        return False
    if item.get("q_market") and markets and item["q_market"] not in markets:
        return False
    if item.get("q_category") and category and item["q_category"] != category:
        return False
    if item.get("q_competitor") and competitors and item["q_competitor"] not in competitors:
        return False
    if item.get("q_keyword") and keywords and item["q_keyword"] not in keywords:
        return False
    return True


_DATE_FULL = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_DATE_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _extract_deadline(text, today, window_end):
    """Best-effort: find an explicit date in the item text that falls in [today, window_end].
    Returns a datetime.date or None. (v1: regex only — Python owns the window decision.)"""
    if not text:
        return None
    for y, mo, da in _DATE_FULL.findall(text):
        try:
            d = datetime.date(int(y), int(mo), int(da))
        except ValueError:
            continue
        if today <= d <= window_end:
            return d
    for mo, da in _DATE_MD.findall(text):
        # A bare M月D日 has no year — assume the current year, but if that lands in the
        # past (e.g. parsing "1月15日" in December), roll to next year so January
        # deadlines aren't silently dropped at a year boundary.
        d = None
        for yr in (today.year, today.year + 1):
            try:
                cand = datetime.date(yr, int(mo), int(da))
            except ValueError:
                break
            if cand >= today:
                d = cand
                break
        if d is not None and d <= window_end:
            return d
    return None


def _md_escape(s):
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()


def personalize_for_watchlist(pool, watchlist, company_name, *, today=None):
    """PHASE B. Filter the shared pool to this watchlist, assign 【N】 anchors, and build
    everything the writer needs. Returns:
        {masthead_md, facts_block, deadlines_md, sources_md, source_rows, anchor_set,
         highlight_count, deadline_count, kept_count}
    """
    today = today or today_date()
    platforms, markets, category, competitors, keywords = _wl_fields(watchlist)
    window_end = today + datetime.timedelta(days=config.mi_deadline_window_days())
    items_by_section = (pool or {}).get("items_by_section") or {}

    # 1) Filter to scope, preserving section order + newest-first within section.
    kept = []
    for section in _SEARCHED_SECTIONS:
        for it in items_by_section.get(section, []):
            if _matches_watchlist(it, platforms, markets, category, competitors, keywords):
                kept.append(it)

    # 2) Assign 【N】 anchors (sequential, in presentation order) + source rows.
    anchor_set = set()
    source_rows = []
    by_section_anchored = {s: [] for s in _SEARCHED_SECTIONS}
    retrieved = today.isoformat()
    for i, it in enumerate(kept, start=1):
        it = dict(it, anchor=i)
        anchor_set.add(i)
        by_section_anchored[it["section"]].append(it)
        source_rows.append({
            "anchor": i, "claim_cn": it["title"] or it["snippet"][:60],
            "source_file": it["source_name"] or it["url"],
            "source_detail": f"{_SECTION_TITLES.get(it['section'], it['section'])} · {it['topic_cn']}",
            "computed_by": "market_intel", "url": it["url"],
            "publish_date": it["publish_date"], "retrieved_date": retrieved})

    # 3) facts_block — the 【N】-anchored, dated lines the writer is given, grouped by
    #    section. Also build a DETERMINISTIC per-section markdown fallback (det_sections)
    #    so the report is complete even if the writer call fails / returns bad JSON.
    def _item_line(it):
        return (f"- 【{it['anchor']}】[{it['topic_cn']}] {it['title']}"
                f"（{it['source_name'] or '来源'}，{it['publish_date']}）"
                f"{('：' + it['snippet'][:140]) if it['snippet'] else ''}")

    facts_lines = []
    det_sections = {}
    for section in _SEARCHED_SECTIONS:
        rows = by_section_anchored[section]
        facts_lines.append(f"\n# {_SECTION_TITLES[section]}")
        if not rows:
            facts_lines.append("（本板块今日无符合时效的情报）")
            det_sections[section] = "无重大变动"
        else:
            lines = [_item_line(it) for it in rows]
            facts_lines.extend(lines)
            det_sections[section] = "\n".join(lines)
    # 竞争态势: 竞品价格 needs a pricing data source — always note it (deferred), under any
    # 竞品动态 we did find.
    det_sections["competition"] = (
        (det_sections.get("competition", "无重大变动") + "\n\n_" + _COMPETITION_PRICE_NOTE + "_")
        if "无重大变动" != det_sections.get("competition")
        else "无重大变动\n\n_" + _COMPETITION_PRICE_NOTE + "_")
    facts_block = "\n".join(facts_lines)
    # Deterministic 今日要点 fallback: the newest few across sections, tagged 参考. When the
    # day is empty, say so plainly (the "no news today" convention).
    all_anchored = [it for s in _SEARCHED_SECTIONS for it in by_section_anchored[s]]
    top = sorted(all_anchored, key=lambda it: it["publish_date"], reverse=True)[:5]
    det_sections["highlights"] = ("\n".join(
        f"- 【参考】【{it['anchor']}】{it['title']}（{it['publish_date']}）" for it in top)
        or "今日暂无重大市场情报。")

    # 4) 截止日期 / 时间敏感 table — date-sorted in PYTHON, within the deadline window.
    deadline_rows = []
    for it in all_anchored:
        d = _extract_deadline(f"{it['title']} {it['snippet']}", today, window_end)
        if d:
            deadline_rows.append((d, it))
    deadline_rows.sort(key=lambda t: t[0])
    if deadline_rows:
        dl = ["| 日期 | 事项 | 类型 | 动作/影响 | 来源 |", "| --- | --- | --- | --- | --- |"]
        for d, it in deadline_rows:
            dl.append(f"| {d.isoformat()} | {_md_escape(it['title'])[:60]} "
                      f"| {_md_escape(_SECTION_TITLES.get(it['section'], ''))} "
                      f"| {_md_escape(it['snippet'])[:60]} | 【{it['anchor']}】 |")
        deadlines_md = "\n".join(dl)
    else:
        deadlines_md = "无重大变动"

    # 5) 数据来源与核验 table (section 8) — source · 链接 · 发布日期 · 检索日期.
    if source_rows:
        sl = ["| # | 来源 | 链接 | 发布日期 | 检索日期 |",
              "| --- | --- | --- | --- | --- |"]
        for r in source_rows:
            sl.append(f"| 【{r['anchor']}】 | {_md_escape(r['source_file'])[:40]} "
                      f"| {_md_escape(r['url'])} | {r['publish_date']} | {r['retrieved_date']} |")
        sources_md = "\n".join(sl)
    else:
        sources_md = "今日无可核验来源。"

    # 6) Masthead (daily).
    win_start, _win_today = coverage_window(today)
    scope = "；".join(p for p in [
        "平台：" + ("、".join(_md_escape(x) for x in platforms) if platforms else "全部"),
        "市场：" + ("、".join(_md_escape(x) for x in markets) if markets else "全部"),
        "类目：" + (_md_escape(category) or "全部")])
    masthead_md = (f"- 覆盖：{win_start.isoformat()} ~ {today.isoformat()}（每日）\n"
                   f"- 店铺/账号：{_md_escape(company_name) or '（未命名）'}\n"
                   f"- 本期覆盖范围：{scope}")

    return {"masthead_md": masthead_md, "facts_block": facts_block,
            "deadlines_md": deadlines_md, "sources_md": sources_md,
            "det_sections": det_sections, "source_rows": source_rows,
            "anchor_set": anchor_set, "highlight_count": len(kept),
            "deadline_count": len(deadline_rows), "kept_count": len(kept),
            "no_news": len(kept) == 0}
