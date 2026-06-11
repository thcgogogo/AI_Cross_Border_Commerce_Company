"""
onboarding.py — the wizard's data helpers (Phase 10).

Three jobs, all pure helpers (the routes live in app.py, the funnel rows in
metrics_store):

  read_table_headers(path)     the column names of an uploaded CSV/Excel file —
                               what step 3's KPI confirmation is pre-filled from.
  guess_kpi_mappings(headers)  map those raw headers onto the standing KPI-mapping
                               shape the company brain already uses
                               ({kpi_name_cn, aliases, unit}) via a rule table of
                               common Chinese/English column names. One click on
                               step 3 saves these straight into the profile.
  stage_demo_data(company_id)  "用示例数据体验" — copy the bundled demo CSV into a
                               fresh persisted upload batch (the same place a real
                               upload lands), so step 4's report generation and
                               every later scheduled run read it exactly like
                               customer data. Encrypted at rest like any upload.

Nothing here talks to an LLM — header guessing is a deterministic rule table, so
step 3 is instant and free.
"""

import os
import shutil

from . import crypto, metrics_store

# The bundled sample dataset (a 12-week cross-border e-commerce sales sheet).
ASSETS_DEMO_DIR = os.path.join(metrics_store.PROJECT_ROOT, "assets", "demo")
DEMO_FILE_NAME = "示例销售数据.csv"


def demo_csv_path():
    return os.path.join(ASSETS_DEMO_DIR, DEMO_FILE_NAME)


# ---------------------------------------------------------------------------
# Header sniffing
# ---------------------------------------------------------------------------
def read_table_headers(path):
    """The column names of a CSV/Excel file, as a list of strings.

    CSVs from Chinese tooling are often GBK — try utf-8 first, then gbk. Any
    failure returns [] (step 3 then simply starts empty); a header sniff must
    never break the wizard."""
    import pandas as pd

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(path, nrows=0)
        else:
            try:
                df = pd.read_csv(path, nrows=0, encoding="utf-8-sig")
            except UnicodeDecodeError:
                df = pd.read_csv(path, nrows=0, encoding="gbk")
        return [str(c).strip() for c in df.columns if str(c).strip()]
    except Exception:  # noqa: BLE001 — a bad file just yields no guesses
        return []


# ---------------------------------------------------------------------------
# KPI guessing — a deterministic rule table over common column names
# ---------------------------------------------------------------------------
# (kpi_name_cn, unit, lowercase keywords that mark a header as this KPI).
# First match wins, top-to-bottom — put the more specific rules (客单价, 退款额,
# 广告费) ABOVE the broad ones (销售额, 成本) they textually contain.
_KPI_RULES = (
    ("日期",   "",   ("日期", "时间", "周次", "月份", "date", "week", "month")),
    ("渠道",   "",   ("渠道", "平台", "店铺", "站点", "channel", "platform", "shop")),
    ("客单价", "¥",  ("客单价", "aov", "单均")),
    ("退款额", "¥",  ("退款", "退货金额", "refund")),
    ("广告费", "¥",  ("广告", "推广", "投放", "ad spend", "ads", "acos")),
    ("订单数", "单", ("订单", "单量", "order")),
    ("毛利",   "¥",  ("毛利", "利润", "profit", "margin")),
    ("成本",   "¥",  ("成本", "采购", "cost", "cogs")),
    ("访客数", "人", ("访客", "流量", "uv", "visitor", "session")),
    ("转化率", "%",  ("转化", "conversion", "cvr")),
    ("库存",   "件", ("库存", "stock", "inventory")),
    ("销售额", "¥",  ("销售", "营收", "收入", "金额", "gmv", "sales", "revenue",
                      "amount")),
)
MAX_KPI_GUESS_ROWS = 12


def guess_kpi_mappings(headers):
    """Pre-filled KPI mapping rows for step 3, from raw file headers.

    Recognized headers group under their canonical KPI (several aliases join into
    one row); unrecognized ones pass through as their own row so the user still
    sees every column and can rename/delete it. The shape is exactly what the
    company brain's profile["kpi_mappings"] stores — confirming is a single save."""
    matched = {}      # kpi_name_cn -> {"kpi_name_cn", "aliases": [..], "unit"}
    passthrough = []
    for raw in headers or []:
        header = str(raw).strip()
        if not header:
            continue
        low = header.lower()
        for kpi_cn, unit, keywords in _KPI_RULES:
            if any(k in low for k in keywords):
                row = matched.setdefault(
                    kpi_cn, {"kpi_name_cn": kpi_cn, "aliases": [], "unit": unit})
                if header not in row["aliases"]:
                    row["aliases"].append(header)
                break
        else:
            passthrough.append({"kpi_name_cn": header, "aliases": [header],
                                "unit": ""})

    out = list(matched.values()) + passthrough
    return [{"kpi_name_cn": r["kpi_name_cn"],
             "aliases": ", ".join(r["aliases"]),
             "unit": r["unit"]} for r in out[:MAX_KPI_GUESS_ROWS]]


# ---------------------------------------------------------------------------
# Demo data staging
# ---------------------------------------------------------------------------
def stage_demo_data(company_id):
    """Copy the bundled demo CSV into a FRESH persisted upload batch for this
    company — the exact place a real upload lands — so step 4 and any scheduled
    "latest_upload" job consume it like customer data. Headers are read BEFORE
    the at-rest encryption pass. Returns {"file_name", "headers", "path"};
    raises FileNotFoundError if the bundled asset is missing (a packaging bug
    worth failing loudly on)."""
    src = demo_csv_path()
    if not os.path.isfile(src):
        raise FileNotFoundError("bundled demo CSV missing: %s" % src)
    batch_dir = metrics_store.new_upload_batch_dir(company_id)
    dest = os.path.join(batch_dir, DEMO_FILE_NAME)
    shutil.copy2(src, dest)
    headers = read_table_headers(dest)
    crypto.encrypt_file_in_place(dest)   # at-rest parity with real uploads
    return {"file_name": DEMO_FILE_NAME, "headers": headers, "path": dest}
