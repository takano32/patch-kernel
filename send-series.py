#!/usr/bin/env python3
"""系列名だけで git send-email を回す。

    send-series                 # 設定にある系列を一覧
    send-series arm64-errata    # dry-run（既定）
    send-series arm64-errata --send

宛先は series.json にだけ書く。コマンド行に --to/--cc を並べない。
"""
import argparse
import glob
import json
import os
import subprocess
import sys

DEFAULT_CONFIG = os.path.expanduser(
    "~/opt/patch-kernel/outgoing/arm64-docs/series.json")
REPO = os.path.expanduser("~/GitHub/linux")


def load(path):
    with open(path) as f:
        return json.load(f)


def patches_for(config, name, entry):
    base = os.path.dirname(os.path.abspath(config))
    if entry.get("patches"):
        return sorted(glob.glob(os.path.join(base, entry["patches"], "*.patch")))
    return sorted(glob.glob(os.path.join(base, name, "*.patch")))


def check_signed_off(patches):
    """cover letter 以外に Signed-off-by があるか。無ければ送らない。"""
    missing = []
    for p in patches:
        if "0000-cover-letter" in os.path.basename(p):
            continue
        text = open(p, encoding="utf-8", errors="replace").read()
        head = text.split("\n---\n", 1)[0]
        if "Signed-off-by: TAKANO Mitsuhiro" not in head:
            missing.append(os.path.basename(p))
    return missing


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("series", nargs="*", help="系列名（省略時は一覧）")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--send", action="store_true", help="実際に送る")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="コマンドを表示するだけ")
    args = ap.parse_args()

    config = load(args.config)
    if not args.series:
        width = max(len(k) for k in config)
        for name, entry in config.items():
            print(f"{name:<{width}}  {len(patches_for(args.config, name, entry)):>2} 通"
                  f"  To: {', '.join(entry.get('to', []))}")
        return 0

    rc = 0
    for name in args.series:
        if name not in config:
            print(f"{name}: 設定にない（{', '.join(config)}）", file=sys.stderr)
            rc = 1
            continue
        entry = config[name]
        patches = patches_for(args.config, name, entry)
        if not patches:
            print(f"{name}: パッチが見つからない", file=sys.stderr)
            rc = 1
            continue
        missing = check_signed_off(patches)
        if missing:
            print(f"{name}: Signed-off-by が無いので送らない:", file=sys.stderr)
            for m in missing:
                print(f"  {m}", file=sys.stderr)
            print("  先に build.sh を実行すること", file=sys.stderr)
            rc = 1
            continue
        cmd = ["git", "send-email", "--confirm=never"]
        for addr in entry.get("to", []):
            cmd += ["--to", addr]
        for addr in entry.get("cc", []):
            cmd += ["--cc", addr]
        if not args.send:
            cmd += ["--dry-run"]
        cmd += patches

        print(f"== {name}  ({len(patches)} 通, {'本送信' if args.send else 'dry-run'})")
        if args.print_only:
            print(cmd)
            continue
        r = subprocess.run(cmd, cwd=args.repo)
        if r.returncode:
            rc = r.returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
