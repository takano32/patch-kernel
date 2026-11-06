#!/usr/bin/env python3
"""系列名だけで git send-email を回す（宛先は設定ファイルにだけ書く）。

    send-series.py                      # 一覧
    send-series.py arm64-errata         # dry-run（既定）
    send-series.py arm64-errata --send  # 本送信

終了コード: 0=成功 / 2=設定の誤り / 3=署名や cover の不整合 / 4=パッチ0件 /
5=git を起動できない。
"""
import argparse
import glob
import json
import os
import quopri
import re
import shlex
import stat
import subprocess
import sys

DEFAULT_CONFIG = os.path.expanduser(
    "~/opt/patch-kernel/outgoing/arm64-docs/series.json")
REPO = os.path.expanduser("~/GitHub/linux")

OK, CFG, SOB, EMPTY, GIT = 0, 2, 3, 4, 5

ADDR = re.compile(r"^[^<>@\s,]+@[^<>@\s,]+$")
NAMED = re.compile(r"^.+ <[^<>@\s,]+@[^<>@\s,]+>$")
SOB_RE = re.compile(r"^Signed-off-by:\s+.*<takano32@gmail\.com>\s*$",
                    re.M | re.I)
COVER_SUBJECT = re.compile(r"^Subject: \[PATCH[^\]]*\b0/(\d+)\]", re.M)
MBOX_FROM = re.compile(
    r"^From \S+ (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d+ [\d:]+ \d{4}",
    re.M)


def fail(code, *lines):
    for line in lines:
        print(line, file=sys.stderr)
    return code


def load(path):
    try:
        st = os.stat(path)
    except OSError as e:
        sys.exit(fail(CFG, f"設定を開けない: {path}: {e}"))
    if not stat.S_ISREG(st.st_mode):
        sys.exit(fail(CFG, f"設定が通常ファイルでない: {path}"))
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(fail(CFG, f"設定が壊れている: {path}: {e}"))
    except (OSError, UnicodeDecodeError) as e:
        sys.exit(fail(CFG, f"設定を読めない: {path}: {e}"))
    if not isinstance(cfg, dict):
        sys.exit(fail(CFG, f"系列名をキーにした辞書であること: {path}"))
    return cfg


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
    sub = entry.get("patches")
    if sub is not None:
        if not isinstance(sub, str):
            problems.append(f"{name}: patches は文字列で書く")
        elif any(c in sub for c in "*?[") or ".." in sub or sub.startswith("/"):
            problems.append(
                f"{name}: patches は設定ディレクトリからの単純な名前だけ: {sub!r}")
    for key in entry:
        if key not in ("patches", "to", "cc"):
            problems.append(f"{name}: 知らないキー {key!r}（綴りを確認）")
    return problems


def series_dir(config_path, name, entry):
    base = os.path.dirname(os.path.realpath(config_path))
    return os.path.join(base, entry.get("patches") or name)


def patches_for(config_path, name, entry):
    def key(p):
        return [int(t) if t.isdigit() else t
                for t in re.split(r"(\d+)", os.path.basename(p))]
    return sorted(glob.glob(os.path.join(series_dir(config_path, name, entry),
                                         "*.patch")), key=key)


def stray_patches(config_path, name, entry):
    """選ばれなかった .patch（隠しファイルなど）を返す。"""
    d = series_dir(config_path, name, entry)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    chosen = {os.path.basename(p)
              for p in patches_for(config_path, name, entry)}
    return sorted(n for n in names if n.endswith(".patch") and n not in chosen)


def read_patch(path):
    """1メールだけの通常ファイルを受け付ける。駄目なら (None, 理由)。"""
    try:
        st = os.stat(path)
    except OSError as e:
        return None, f"stat できない: {e.strerror}"
    if not stat.S_ISREG(st.st_mode):
        return None, "通常ファイルでない"
    if st.st_size == 0:
        return None, "空ファイル"
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return None, f"読めない: {e.strerror}"
    if b"\0" in raw:
        return None, "NUL バイト入り"
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    if len(MBOX_FROM.findall(text)) > 1:
        return None, "1ファイルに複数メール"
    return text, ""


def commit_message(text):
    """ヘッダを除いたコミットメッセージ。区切りが無ければ None。"""
    head, sep, rest = text.partition("\n\n")
    if not sep:
        return None
    if re.search(r"^Content-Transfer-Encoding: quoted-printable", head,
                 re.M | re.I):
        try:
            rest = quopri.decodestring(
                rest.encode("utf-8", "replace")).decode("utf-8", "replace")
        except Exception:
            return None
    m = re.search(r"^(?:---|diff --git .*)$", rest, re.M)
    if not m:
        return None
    return rest[:m.start()]


def is_cover(path, text):
    """cover は「差分を持たないメール」だけ。件名やファイル名では決めない
    （--start-number=0 や 0000-*.patch という名前の実パッチを巻き込まないため）。"""
    return not re.search(r"^diff --git ", text, re.M)


def check_series(patches):
    """署名と cover の整合を検査する。問題の説明を並べて返す。"""
    problems, covers, bodies, total = [], 0, 0, -1
    for p in patches:
        name = os.path.basename(p)
        text, err = read_patch(p)
        if text is None:
            problems.append(f"{name}: {err}")
            continue
        if is_cover(p, text):
            covers += 1
            m = COVER_SUBJECT.search(text)
            if m:
                total = int(m.group(1))
            continue
        bodies += 1
        msg = commit_message(text)
        if msg is None:
            problems.append(f"{name}: コミットメッセージの区切りが見つからない")
        elif not SOB_RE.search(msg):
            problems.append(f"{name}: Signed-off-by が無い")
    if covers > 1:
        problems.append(f"cover letter が {covers} 個ある")
    if covers == 1 and total >= 0 and total != bodies:
        problems.append(f"cover letter は {total} 通だが、実パッチは {bodies} 通")
    return problems


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

    # 先に全系列を検査する（後ろの設定誤りに気づく前に前を送らないため）
    plan, rc = [], OK
    seen = set()
    for name in args.series:
        if name in seen:
            continue
        seen.add(name)
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
            rc = fail(EMPTY,
                      f"{name}: パッチが0件: {series_dir(args.config, name, entry)}")
            continue
        stray = stray_patches(args.config, name, entry)
        if stray:
            rc = fail(EMPTY, f"{name}: 送らない .patch が混じっている:",
                      *["  " + s for s in stray])
            continue
        problems = check_series(patches)
        if problems:
            rc = fail(SOB, f"{name}: 送れない:",
                      *["  " + m for m in problems],
                      "  build.sh で署名してからやり直すこと")
            continue
        plan.append((name, entry, patches))

    if rc != OK:
        return rc

    for name, entry, patches in plan:
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
            r = subprocess.run(cmd, cwd=args.repo, timeout=1800)
        except subprocess.TimeoutExpired:
            rc = fail(GIT, f"{name}: git send-email が 30 分で終わらなかった")
            continue
        except (OSError, FileNotFoundError) as e:
            rc = fail(GIT, f"{name}: git を起動できない: {e}")
            continue
        if r.returncode:
            rc = r.returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
