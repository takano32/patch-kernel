#!/usr/bin/env python3
"""系列名だけで git send-email を回す（宛先は設定ファイルにだけ書く）。

    send-series.py                      # 一覧
    send-series.py arm64-errata         # dry-run（既定）
    send-series.py arm64-errata --send  # 本送信

実行は毎回 logs/<YYYYmmdd-HHMMSS>.log に残る（コマンド・出力・rc・
Message-ID・送ったパッチの sha256）。

終了コード: 0=成功 / 2=設定の誤り / 3=署名や cover の不整合 / 4=パッチ0件 /
5=git を起動できない。
"""
import argparse
import datetime
import email
import glob
import hashlib
import imaplib
import json
import os
import quopri
import re
import shlex
import stat
import subprocess
import sys
import threading

DEFAULT_CONFIG = os.path.expanduser(
    "~/opt/patch-kernel/outgoing/arm64-docs/series.json")
REPO = os.path.expanduser("~/GitHub/linux")
LOG_DIR = os.path.expanduser("~/opt/patch-kernel/logs")

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
        logline(line)
    logline(f"# rc={code}")
    return code


LOG = None
LOG_PATH = None


def logline(text):
    if LOG is None:
        return
    LOG.write(str(text).rstrip("\n") + "\n")
    LOG.flush()


def open_log(tag):
    """logs/<YYYYmmdd-HHMMSS>.log を開く。失敗しても送信は止めない。"""
    global LOG, LOG_PATH
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        LOG_PATH = os.path.join(LOG_DIR, f"{stamp}.log")
        LOG = open(LOG_PATH, "w", encoding="utf-8")
    except OSError as e:
        LOG, LOG_PATH = None, None
        print(f"警告: ログを開けない（記録なしで続行）: {e}", file=sys.stderr)
        return
    logline(f"# {datetime.datetime.now().isoformat(timespec='seconds')}")
    logline(f"# {tag} pid={os.getpid()} argv={shlex.join(sys.argv)}")


def run_logged(cmd, cwd, timeout=1800):
    """git send-email を実行し、出力を画面とログの両方へ流す。

    (returncode, timed_out) を返す。起動できなければ例外を投げる。
    """
    logline("$ " + shlex.join(cmd))
    print("$ " + shlex.join(cmd), flush=True)
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    timed_out = threading.Event()

    def kill():
        timed_out.set()
        p.kill()

    timer = threading.Timer(timeout, kill)
    timer.daemon = True
    timer.start()
    buf = []
    assert p.stdout is not None
    try:
        for line in p.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            logline(line)
            buf.append(line)
        rc = p.wait()
    finally:
        timer.cancel()
    logline(f"# rc={rc}" + (" (timeout)" if timed_out.is_set() else ""))
    return rc, timed_out.is_set(), "".join(buf)


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


def sha256(path):
    """送った差分を後から突き合わせるための指紋。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError as e:
        return f"(読めない: {e.strerror})"


SEND_HOME = "/home/takano32"
IMAP_HOST = "imap.gmail.com"


def _git_global(key):
    """Gmail の設定は実 HOME 側にだけある（.home には無い）。"""
    try:
        out = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "HOME": SEND_HOME})
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def diff_digest(text):
    """+- 行だけの指紋。件名が変わっても同じ差分なら一致する。"""
    body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    lines = []
    for line in body.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            lines.append(line.rstrip())
    if not lines:
        return ""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _sent_folder(m):
    typ, boxes = m.list()
    for b in boxes or []:
        line = b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)
        if "\\Sent" in line:
            return line.split(' "/" ')[-1].strip().strip('"')
    return None


def sent_duplicates(patches):
    """Gmail の Sent を読み、同じ +- 差分が既に出ていれば Message-ID を返す。

    件名ではなく差分で照合するので、件名を直して送り直しても重複を検出する。
    """
    local = {}
    for p in patches:
        text, _ = read_patch(p)
        if text is None or is_cover(p, text):
            continue
        d = diff_digest(text)
        if d:
            local[d] = os.path.basename(p)
    if not local:
        return None
    user = _git_global("sendemail.smtpuser")
    pw = _git_global("sendemail.smtppass")
    if not user or not pw:
        return None
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=20)
        m.login(user, pw)
    except Exception:
        return None
    try:
        box = _sent_folder(m)
        if not box:
            return None
        typ, _ = m.select(f'"{box}"', readonly=True)
        if typ != "OK":
            return None
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=7)).strftime("%d-%b-%Y")
        typ, ids = m.search(None, "FROM", f'"{user}"', "SINCE", since)
        if typ != "OK" or not ids or not ids[0]:
            return None
        found = {}
        for i in ids[0].split():
            typ, d = m.fetch(i, "(BODY.PEEK[])")
            if typ != "OK" or not d or not d[0]:
                continue
            raw = d[0][1]
            if isinstance(raw, tuple):
                raw = raw[0]
            if not isinstance(raw, bytes):
                continue
            msg = email.message_from_bytes(raw)
            payload = msg.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            dg = diff_digest(payload.decode("utf-8", "replace"))
            if dg in local:
                found[dg] = (msg.get("Message-ID") or "").strip()
        if found and len(found) == len(local):
            return next(iter(found.values()))
        return None
    finally:
        try:
            m.logout()
        except Exception:
            pass


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
    ap.add_argument("--force-duplicate", action="store_true",
                    help="Sent に同じ差分があっても送る（v2 以外では使わない）")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="コマンドを表示するだけ")
    args = ap.parse_args()

    if not args.print_only and args.series:
        open_log("本送信" if args.send else "dry-run")
    try:
        return run(args)
    finally:
        if LOG is not None:
            LOG.close()
            print(f"log: {LOG_PATH}")


def run(args):
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

    # 送る前に、同じ +- 差分が Gmail の Sent に無いかを必ず見る。
    # 件名ではなく差分で照合するので、件名を直した再送も止まる。
    if not args.print_only:
        duplicates = {}
        for name, entry, patches in plan:
            mid = sent_duplicates(patches)
            if mid:
                duplicates[name] = mid
                print(f"!! {name}: 同じ差分が既に Sent にあります {mid}",
                      flush=True)
                logline(f"!! {name}: duplicate of {mid}")
        if duplicates and args.send and not args.force_duplicate:
            return fail(SOB, "既に送った差分があるので送らない:",
                        *[f"  {n}: {m}" for n, m in duplicates.items()],
                        "  v2 として送り直す場合だけ --force-duplicate")

    for name, entry, patches in plan:
        cmd = ["git", "send-email", "--confirm=never", "--suppress-cc=all"]
        for a in entry.get("to", []):
            cmd += ["--to", a]
        for a in entry.get("cc", []):
            cmd += ["--cc", a]
        if not args.send:
            cmd += ["--dry-run"]
        cmd += patches

        label = "本送信" if args.send else "dry-run"
        print(f"== {name}  ({len(patches)} 通, {label})", flush=True)
        logline(f"== {name}  ({len(patches)} 通, {label})")
        for p in patches:
            logline(f"   {os.path.basename(p)}  sha256={sha256(p)}")
        if args.print_only:
            print(shlex.join(cmd))
            continue
        try:
            code, timed_out, out = run_logged(cmd, args.repo)
        except (OSError, FileNotFoundError) as e:
            rc = fail(GIT, f"{name}: git を起動できない: {e}")
            continue
        if timed_out:
            rc = fail(GIT, f"{name}: git send-email が 30 分で終わらなかった")
            continue
        ids = re.findall(r"^Message-I[Dd]:\s*<([^>]+)>", out, re.M)
        for mid in dict.fromkeys(ids):
            logline(f"# {name}: sent <{mid}>")
        if code:
            rc = code
            break   # 失敗したら次の系列に進まない（部分送信を防ぐ）
    logline(f"# 完了 rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
