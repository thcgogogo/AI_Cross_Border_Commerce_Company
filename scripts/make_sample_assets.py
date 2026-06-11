"""Build the landing-page sample-report showcase assets (redesign Phase 3).

Takes a real, pipeline-generated report PDF and renders every page to PNG for
frontend/assets/sample/, plus a manifest.json the landing page reads:

    {"pdf": "sample-report.pdf", "pages": N, "sources_page": K}

`sources_page` is the 1-based page containing the 数据来源与核验 appendix (used
as the illustration in the landing's 数字可溯源 block); absent if not found.

Usage (regenerate any time, e.g. after a rebrand — Phase 10 reruns this):
    .venv/bin/python scripts/make_sample_assets.py --pdf <report.pdf> \
        --out frontend/assets/sample

Requires pymupdf (dev-only dependency):  .venv/bin/pip install pymupdf
"""
import argparse
import json
import os
import shutil

import fitz  # pymupdf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="source report PDF (real pipeline output)")
    ap.add_argument("--out", default="frontend/assets/sample")
    ap.add_argument("--zoom", type=float, default=2.0, help="render scale (2.0 ≈ 1190px wide)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    # Clear stale page images so a shorter regenerated PDF leaves no orphans.
    for f in os.listdir(args.out):
        if f.startswith("page-") and f.endswith(".png"):
            os.remove(os.path.join(args.out, f))

    doc = fitz.open(args.pdf)
    sources_page = None
    mat = fitz.Matrix(args.zoom, args.zoom)
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=mat)
        out_png = os.path.join(args.out, "page-%d.png" % i)
        pix.save(out_png)
        if sources_page is None and "数据来源与核验" in page.get_text():
            sources_page = i
        print("rendered", out_png)

    pdf_name = "sample-report.pdf"
    shutil.copyfile(args.pdf, os.path.join(args.out, pdf_name))

    manifest = {"pdf": pdf_name, "pages": doc.page_count}
    if sources_page:
        manifest["sources_page"] = sources_page
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("manifest:", manifest)


if __name__ == "__main__":
    main()
