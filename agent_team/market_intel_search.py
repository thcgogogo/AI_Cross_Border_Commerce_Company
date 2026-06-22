"""
market_intel_search.py — the swappable web-search backend for 市场情报周报.

ONE provider-agnostic entry point — `search(query, *, start_date, max_results)` — returns a
list of NORMALIZED, structured results:

    {"title", "url", "snippet", "publish_date" (datetime.date | None), "source_name"}

A structured, DATE-STAMPED result is the whole point: the engine can only gate recency in
Python (config.mi_recency_days) if every result carries a real publish_date. That is why this
layer uses a structured search API (default 博查 Bocha, mainland-reachable) and NOT Qwen
`enable_search` / Claude native search, which return model-browsed prose with no dates to filter.

Backends (config.mi_search_backend()):
  - "bocha" — 博查 AI Search (api.bochaai.com). Reads MI_SEARCH_API_KEY / MI_SEARCH_BASE_URL.
  - "mock"  — a deterministic canned set (for `MI_SEARCH_BACKEND=mock python app.py` smoke runs);
              tests inject their own via set_mock_results().

Every backend DEGRADES GRACEFULLY: a missing key, a network error, or an unparseable response
returns [] (that topic simply contributes nothing — a section may end up "无重大变动"). The
caller never crashes a weekly run because one query failed.
"""
import datetime

from . import config


# ---------------------------------------------------------------------------
# Normalization (one place, used by every backend)
# ---------------------------------------------------------------------------
def _parse_date(value):
    """Coerce a backend date value into a datetime.date, or None if unparseable.
    Tolerates ISO 8601 with/without time/zone ('2026-06-15', '2026-06-15T00:00:00Z')."""
    if not value:
        return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.datetime):
        return value.date()
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("Z", "").replace("/", "-")
    # Try full ISO first, then a plain date prefix.
    for candidate in (s, s[:10]):
        try:
            return datetime.date.fromisoformat(candidate[:10])
        except ValueError:
            continue
    return None


def _normalize_item(raw):
    """Coerce one raw backend hit into the canonical shape. publish_date becomes a
    datetime.date or None; everything else a trimmed string."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or raw.get("name") or "").strip()
    url = str(raw.get("url") or "").strip()
    snippet = str(raw.get("snippet") or raw.get("summary") or "").strip()
    source = str(raw.get("source_name") or raw.get("siteName")
                 or raw.get("site_name") or "").strip()
    publish_date = _parse_date(raw.get("publish_date") or raw.get("datePublished")
                               or raw.get("dateLastCrawled"))
    if not (title or snippet) or not url:
        return None
    return {"title": title, "url": url, "snippet": snippet,
            "publish_date": publish_date, "source_name": source}


def _normalize_many(rows, max_results):
    out = []
    for r in rows or []:
        item = _normalize_item(r)
        if item:
            out.append(item)
        if len(out) >= max_results:
            break
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def search(query, *, start_date=None, max_results=None):
    """Run one search and return normalized results. Never raises — returns [] on any error.
    `start_date` (datetime.date|None) is a freshness HINT passed to backends that accept one;
    the authoritative recency gate is the engine's publish_date filter, not this."""
    if max_results is None:
        max_results = config.mi_max_results_per_query()
    backend = config.mi_search_backend()
    try:
        if backend == "mock":
            return _mock_search(query, start_date=start_date, max_results=max_results)
        if backend == "bocha":
            return _bocha_search(query, start_date=start_date, max_results=max_results)
    except Exception:
        return []   # graceful: a failed query contributes nothing to the pool
    return []   # unknown backend id


# ---------------------------------------------------------------------------
# 博查 Bocha AI Search
# ---------------------------------------------------------------------------
def _bocha_search(query, *, start_date, max_results):
    key = config.mi_search_api_key()
    if not key:
        return []
    try:
        import requests
    except ImportError:
        return []
    # 博查's freshness param accepts a recency label; map OUR window (config, not the real
    # system clock — keeps the hint coupled to the engine's window and deterministic in tests)
    # to its buckets. The authoritative gate is still the engine's publish_date filter.
    days = config.mi_recency_days()
    freshness = "oneDay" if days <= 1 else "oneWeek" if days <= 7 else "oneMonth"
    resp = requests.post(
        config.mi_search_base_url().rstrip("/") + "/web-search",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"query": query, "freshness": freshness, "summary": True,
              "count": max_results},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json() or {}
    # Bing-compatible shape: data.data.webPages.value[]
    pages = (((data.get("data") or {}).get("webPages") or {}).get("value")) or []
    return _normalize_many(pages, max_results)


# ---------------------------------------------------------------------------
# Mock backend (smoke runs + a default tests can override)
# ---------------------------------------------------------------------------
_MOCK_RESULTS = None   # tests set: a list[dict] OR a callable(query) -> list[dict]


def set_mock_results(results):
    """Tests: inject canned raw results (list or callable). Pass None to restore the default."""
    global _MOCK_RESULTS
    _MOCK_RESULTS = results


def _mock_search(query, *, start_date, max_results):
    if _MOCK_RESULTS is not None:
        rows = _MOCK_RESULTS(query) if callable(_MOCK_RESULTS) else list(_MOCK_RESULTS)
        return _normalize_many(rows, max_results)
    # Default canned set for `MI_SEARCH_BACKEND=mock python app.py`: two FRESH items (today)
    # + one deliberately STALE 2024 item, so the recency gate visibly drops the stale one.
    today = config.current_date_str()
    stale_year = str(int(today[:4]) - 2)
    rows = [
        {"title": f"[模拟] {query} — 最新动态", "url": "https://example.com/fresh-1",
         "snippet": "模拟的当周情报条目（用于演示）。", "publish_date": today,
         "source_name": "example.com"},
        {"title": f"[模拟] {query} — 报名截止 {today}", "url": "https://example.com/fresh-2",
         "snippet": f"报名截止日期 {today}。", "publish_date": today,
         "source_name": "sellercentral.amazon.com"},
        {"title": f"[模拟-过期] {query}（{stale_year} 旧闻）", "url": "https://example.com/stale",
         "snippet": "这条应被时效闸门丢弃。", "publish_date": f"{stale_year}-06-01",
         "source_name": "example.com"},
    ]
    return _normalize_many(rows, max_results)
