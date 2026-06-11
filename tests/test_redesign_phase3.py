"""UI redesign Phase 3 — 营销首页 acceptance tests (plan §7).

The landing page itself is client-rendered (verified by hand / preview in the
running app); these tests pin down the server-side contract it depends on:

  1. Product identity (D1 = "Tangent AI"): /api/meta carries the brand, the
     tagline and a byline that never reads "…AI AI 团队…", plus the 单次报告
     price knob the pricing section shows (purchasable in Phase 7 / B4).
  2. config.product_byline() handles both name shapes (ends-with-AI or not).
  3. The sample-report showcase assets exist and are served: manifest.json,
     every page-N.png it declares, and the sample PDF — generated from a real
     pipeline run by scripts/make_sample_assets.py.
  4. The landing pricing data: /api/plans is public (logged-out) and carries
     the three brief-aligned plans the cards are built from.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import config  # noqa: E402

SAMPLE_DIR = os.path.join(ROOT, "frontend", "assets", "sample")


# ---------------------------------------------------------------------------
# 1. Product identity in /api/meta
# ---------------------------------------------------------------------------
def test_meta_carries_tangent_ai_identity_and_single_price():
    meta = app.app.test_client().get("/api/meta").get_json()
    assert meta["product_name"] == "Tangent AI"
    assert meta["product_byline"] == "由 Tangent AI 团队生成 · 每个数字可溯源"
    assert "AI AI" not in meta["product_byline"]
    assert meta["single_report_price_yuan"] == 29.0


# ---------------------------------------------------------------------------
# 2. The byline knob for both name shapes
# ---------------------------------------------------------------------------
def test_product_byline_handles_ai_suffix(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "Tangent AI")
    assert config.product_byline() == "由 Tangent AI 团队生成 · 每个数字可溯源"

    monkeypatch.setenv("PRODUCT_NAME", "周一报到")
    assert config.product_byline() == "由 周一报到 AI 团队生成 · 每个数字可溯源"


# ---------------------------------------------------------------------------
# 3. Sample showcase assets exist and are served
# ---------------------------------------------------------------------------
def test_sample_assets_exist_and_match_manifest():
    man_path = os.path.join(SAMPLE_DIR, "manifest.json")
    assert os.path.exists(man_path), (
        "missing sample assets — run scripts/make_sample_assets.py (plan Phase 3)")
    with open(man_path, encoding="utf-8") as f:
        man = json.load(f)

    assert man["pages"] >= 1
    assert os.path.exists(os.path.join(SAMPLE_DIR, man["pdf"]))
    for i in range(1, man["pages"] + 1):
        assert os.path.exists(os.path.join(SAMPLE_DIR, "page-%d.png" % i)), i
    # The 数字可溯源 illustration: the appendix page the brief's trust story
    # leans on must exist in the generated sample.
    assert man.get("sources_page"), "sample PDF lacks the 数据来源与核验 appendix"


def test_sample_assets_served_publicly():
    client = app.app.test_client()
    man = client.get("/assets/sample/manifest.json")
    assert man.status_code == 200
    pdf = client.get("/assets/sample/" + man.get_json()["pdf"])
    assert pdf.status_code == 200
    assert pdf.data[:5] == b"%PDF-"
    page1 = client.get("/assets/sample/page-1.png")
    assert page1.status_code == 200


# ---------------------------------------------------------------------------
# 4. Pricing data the landing cards are built from (public route)
# ---------------------------------------------------------------------------
def test_plans_public_and_brief_aligned():
    data = app.app.test_client().get("/api/plans").get_json()
    plans = {p["code"]: p for p in data["plans"]}
    assert plans["free"]["label"] == "免费体验"
    assert plans["free"]["lifetime_quota"] is True
    assert plans["basic"]["monthly_price_yuan"] == 199.0
    assert plans["pro"]["label"] == "企业版"
    assert plans["pro"]["monthly_price_yuan"] == 599.0
