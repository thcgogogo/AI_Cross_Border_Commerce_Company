#!/usr/bin/env python3
"""
gen_latest_json.py — assemble the Tauri updater's latest.json from the per-target bundles
the release matrix produced. Run by the aggregation job after all three targets build.

Each target dir holds a bundle + its `.sig` (the Ed25519 update signature). Output is
published to the ICP-filed host configured in tauri.conf.json → plugins.updater.endpoints.

Usage:
  python gen_latest_json.py --version 0.1.0 --notes "..." --base-url https://api.tangent.example \
      --target darwin-aarch64:dist/aarch64/Tangent_Agent.app.tar.gz \
      --target darwin-x86_64:dist/x86_64/Tangent_Agent.app.tar.gz \
      --target windows-x86_64:dist/win/Tangent_Agent_x64-setup.nsis.zip \
      > latest.json
"""
import argparse
import json
import os
import sys


def _sig_for(bundle_path):
    sig_path = bundle_path + ".sig"
    if not os.path.isfile(sig_path):
        sys.exit(f"missing update signature: {sig_path} (sign the bundle first)")
    with open(sig_path, encoding="utf-8") as f:
        return f.read().strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--notes", default="")
    ap.add_argument("--base-url", required=True, help="the ICP-filed download host")
    ap.add_argument("--pub-date", default="", help="ISO 8601; stamp at publish time")
    ap.add_argument("--target", action="append", default=[],
                    help="platform:bundle_path (e.g. darwin-aarch64:dist/.../app.tar.gz)")
    args = ap.parse_args()

    platforms = {}
    for spec in args.target:
        platform, _, bundle = spec.partition(":")
        if not platform or not bundle:
            sys.exit(f"bad --target {spec!r}; want platform:bundle_path")
        platforms[platform] = {
            "signature": _sig_for(bundle),
            "url": f"{args.base_url.rstrip('/')}/desktop/{args.version}/{os.path.basename(bundle)}",
        }

    print(json.dumps({
        "version": args.version,
        "notes": args.notes,
        "pub_date": args.pub_date,
        "platforms": platforms,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
