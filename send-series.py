#!/usr/bin/env python3
"""系列名だけで git send-email を回す（宛先は設定ファイルにだけ書く）。

    send-series.py                      # 一覧
    send-series.py arm64-errata         # dry-run（既定）
    send-series.py arm64-errata --send  # 本送信

終了コード: 0=成功 / 2=設定の誤り / 3=Signed-off-by 無し / 4=パッチ0件 /
5=git を起動できない。
"""
import argparse
import glob
import json
import os
import quopri
import re
import shlex
import subprocess
import sys

DEFAULT_CONFIG = os.path.expanduser(
    "~/opt/patch-kernel/outgoing/arm64-docs/series.json")
REPO = os.path.expanduser("~/GitHub/linux")

OK, CFG, SOB, EMPTY, GIT = 0, 2, 3, 4, 5


def fail(code, *lines):
    for line in lines:
        print(line, file=sys.stderr)
    return code


def load(path):
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        sys.exit(fail(CFG, f"設定が無い: {path}"))
    except json.JSONDecodeError as e:
        sys.exit(fail(CFG, f"設定が壊れている: {path}: {e}"))
    if not isinstance(cfg, dict):
        sys.exit(fail(CFG, f"系列名をキーにした辞書であること: {path}"))
    return cfg


ADDR = re.compile(r"^[^<>@\s,]+@[^<>@\s,]+\.[^<>@\s,]+$")
NAMED = re.compile(r"^.+ <[^<>@\s,]+@[^<>@\s,]+\.[^<>@\s,]+>$")


def validate_entry(name, entry):
    """1系列の宛先を検査する。空の to は「Cc だけに届く」経路なので止める。"""
    problems = []
    if not isinstance(entry, dict):
        return [f"{name}: 値が辞書でない"]
    for key in ("to", "cc"):
        value = entry.get(key, [])
        if not isinstance(value, list):
            problems.append(
                f"{name}: {key} は配列で書く（文字列だと1文字ずつ宛先になる）")
            continue
        for a in value:
            if not isinstance(a, str) or not (ADDR.match(a) or NAMED.match(a)):
                problems.append(f"{name}: {key} のアドレスとして読めない: {a!r}")
    if not entry.get("to"):
        problems.append(f"{name}: to が空（To 無しで送ると Cc だけに届く）")
    return problems


def patches_for(config_path, name, entry):
    base = os.path.dirname(os.path.abspath(config_path))
    sub = entry.get("patches") or name
    return sorted(glob.glob(os.path.join(base, sub, "*.patch")))


def is_cover(path, text):
    if "0000-cover-letter" in os.path.basename(path):
        return True
    return bool(re.search(r"^Subject: \[PATCH 0/\d+\]", text, re.M))


def commit_message(text):
    """パッチ先頭のコミットメッセージ部分。区切りが見つからなければ None。"""
    text = text.replace("\r\n", "\n")
    if re.search(r"^Content-Transfer-Encoding: quoted-printable",
                 text, re.M | re.I):
        head, _, body = text.partition("\n\n")
        text = head + "\n\n" + quopri.decodestring(body).decode("utf-8", "replace")
    m = re.search(r"^(?:---|diff --git .*)$", text, re.M)
    if not m:
        return None
    return text[:m.start()]


SOB_RE = re.compile(
    r"^signed-off-by\s*:\s*.*(takano32@gmail\.com|TAKANO Mitsuhiro)", re.M | re.I)


def missing_sob(patches):
    """SoB が無いパッチを返す。判定できないものも「無い」に倒す（fail closed）。"""
    missing = []
    for p in patches:
        text = open(p, encoding="utf-8", errors="replace").read()
        if is_cover(p, text):
            continue
        msg = commit_message(text)
        if msg is None or not SOB_RE.search(msg):
            missing.append(os.path.basename(p))
    return missing


def list_series(config_path, cfg):
    code = OK
    width = max((len(k) for k in cfg), default=0)
    for name, entry in cfg.items():
        patches = patches_for(config_path, name, entry)
        if not patches:
            code = EMPTY
        print(f"{name:<{width}}  {len(patches):>2} 通"
              f"  To: {', '.join(entry.get('to', [])) or '(空)'}")
    return code


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("series", nargs="*", help="系列名（省略時は一覧）")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--send", action="store_true", help="実際に送る")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="コマンドを表示するだけ")
    args = ap.parse_args()

    cfg = load(args.config)

    if not args.series:
        if args.send:
            return fail(CFG, "--send には系列名を付ける（何も送らずに終わるのを防ぐ）")
        problems = [p for n, e in cfg.items() for p in validate_entry(n, e)]
        if problems:
            return fail(CFG, *["設定の誤り:"] + ["  " + p for p in problems])
        return list_series(args.config, cfg)

    rc = OK
    for name in args.series:
        entry = cfg.get(name)
        if entry is None:
            rc = fail(CFG, f"{name}: 設定にない（{', '.join(cfg)}）")
            continue
        problems = validate_entry(name, entry)
        if problems:
            rc = fail(CFG, *["設定の誤り:"] + ["  " + p for p in problems])
            continue
        patches = patches_for(args.config, name, entry)
        if not patches:
            rc = fail(EMPTY, f"{name}: パッチが0件: "
                             f"{os.path.dirname(os.path.abspath(args.config))}/"
                             f"{entry.get('patches') or name}")
            continue
        missing = missing_sob(patches)
        if missing:
            rc = fail(SOB, f"{name}: Signed-off-by が無いので送らない:",
                      *["  " + m for m in missing],
                      "  build.sh で署名してからやり直すこと")
            continue

        cmd = ["git", "send-email", "--confirm=never", "--suppress-cc=all"]
        for a in entry.get("to", []):
            cmd += ["--to", a]
        for a in entry.get("cc", []):
            cmd += ["--cc", a]
        if not args.send:
            cmd += ["--dry-run"]
        cmd += patches

        print(f"== {name}  ({len(patches)} 通, "
              f"{'本送信' if args.send else 'dry-run'})", flush=True)
        if args.print_only:
            print(shlex.join(cmd))
            continue
        try:
            r = subprocess.run(cmd, cwd=args.repo)
        except (OSError, FileNotFoundError) as e:
            rc = fail(GIT, f"{name}: git を起動できない: {e}")
            continue
        if r.returncode:
            rc = r.returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
