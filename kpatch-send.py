#!/usr/bin/python3
"""カーネルパッチを系列ごとに送出する汎用ツール（重複チェック付き）。

設定ファイル（既定 ~/GitHub/kpatch-series.json）に base / 宛先 / 系列を書き、
系列名を引数で選ぶ。既定は dry-run、--send で本送信。

    kpatch-send.py                 # 設定の全系列を dry-run
    kpatch-send.py --send          # 全系列を本送信
    kpatch-send.py stat            # stat 系列だけ dry-run
    kpatch-send.py --base auto stat

系列ごとにやること:
  1. 送信用ブランチを作り直して Signed-off-by を付ける
  2. format-patch（reroll 指定があれば -v N）
  3. checkpatch --strict（0/0/0 でなければ中止）
  4. クリーンな worktree に git am（失敗なら中止）
  5. lore ミラーで同じ diff が既に出ていないか照合（出ていれば中止）
  6. git send-email（--send のときだけ実送信）
"""

import argparse
import email
import fcntl
import glob
import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# 環境変数の HOME はセッションごとに変わるので、実ユーザーのホームを使う
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
HOME = REAL_HOME
# 設定とログはスクリプトと同じディレクトリに置く（どこに移しても動く）
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DEFAULT = os.path.join(TOOL_DIR, "config.json")

T0 = time.time()
STEP = 0
TOTAL = 0
SKIPPED = []
LOG = None
LOG_PATH = None


def say(msg="", end="\n"):
    """画面とログの両方に出す。"""
    print(msg, end=end, flush=True)
    if LOG:
        LOG.write(msg + end)
        LOG.flush()


def log_block(text):
    """コマンドの生出力は全部ログに落とす。"""
    if LOG and text:
        LOG.write(text if text.endswith("\n") else text + "\n")
        LOG.flush()


def clock():
    return time.time() - T0


class Fatal(Exception):
    pass


def head(msg):
    global STEP
    STEP += 1
    say(f"\n[{STEP}/{TOTAL}] {msg}   ({clock():.1f}s)")
    return time.time()


def ok(msg, started):
    say(f"       ok  {msg}   (+{time.time() - started:.1f}s / 合計 {clock():.1f}s)")


class Job:
    """1回の実行に必要な文脈（リポジトリ・環境・設定）。"""

    def __init__(self, cfg, args):
        self.repo = os.path.expanduser(cfg.get("repo", f"{HOME}/GitHub/linux"))
        self.env = {**os.environ, "HOME": HOME}
        self.list = args.list or cfg["list"]
        self.mirror = (args.mirror or cfg.get("mirror")
                       or "https://lore-kernel.gnuweeb.org/"
                       + self.list.split("@")[0])
        self.to = args.to or cfg.get("to", [])
        self.cc = args.cc or cfg.get("cc", [])
        base_dir = os.path.expanduser(args.workdir or cfg.get(
            "workdir", "/tmp/kpatch"))
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        self.workdir = os.path.join(base_dir, self.run_id)
        self.base = args.base or cfg.get("base", "auto")
        self.send = args.send
        self.keep = args.keep
        # 名前を指定せず --send / --pending のときは「未送信のもの」だけ扱う
        self.pending_only = bool(getattr(args, "pending", False)
                                 or (args.send and not args.series))

    # ---- 基本操作 ----
    def git(self, cmd, check=True, capture=True):
        say(f"       $ git {' '.join(cmd)}")
        p = subprocess.run(["git"] + cmd, cwd=self.repo, env=self.env,
                           capture_output=capture, text=True)
        if capture:
            log_block(f"--- rc={p.returncode}\n{p.stdout}{p.stderr}")
            for line in (p.stdout + p.stderr).splitlines():
                if any(s in line for s in ("0001-", "Executing", "Applying:",
                                           "Successfully rebased", "fatal",
                                           "error:")):
                    say(f"         {line}")
        if check and p.returncode != 0:
            raise Fatal(f"git {' '.join(cmd)} (rc={p.returncode})\n"
                        + (p.stdout + p.stderr)[-800:])
        return p

    def resolve_base(self, branch):
        if self.base != "auto":
            return self.base
        p = subprocess.run(["git", "merge-base", branch, "origin/master"],
                           cwd=self.repo, env=self.env, capture_output=True,
                           text=True)
        if p.returncode != 0:
            raise Fatal("--base auto で merge-base が取れません。--base を指定してください")
        return p.stdout.strip()

    # ---- 重複チェック ----
    def fetch(self, url):
        last = None
        for _ in range(2):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read().decode("utf-8", "replace")
            except Exception as exc:
                last = exc
                time.sleep(1)
        raise last

    @staticmethod
    def diff_digest(text):
        i = text.find("diff --git ")
        if i < 0:
            return None
        body = re.split(r"\n-- ?\n", text[i:])[0]
        lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()

    def check_duplicate(self, patch_path, subject, author):
        local = self.diff_digest(open(patch_path, encoding="utf-8").read())
        words = re.sub(r"^(\[[^\]]*\]\s*)+", "", subject).strip()
        phrase = " ".join(words.split()[:6])
        say(f"       検索語: {phrase!r}")
        # 件名検索は記号や語の切れ方で自分の投稿を落とすことがあるので、
        # 著者検索も足して候補を必ず網羅する
        words_clean = re.sub(r"[^0-9A-Za-z ]+", " ", phrase)
        ids = set()
        for q in (" ".join(words_clean.split()[:5]), "f:takano32"):
            html = self.fetch(f"{self.mirror}/?q={urllib.parse.quote(q)}")
            found = set(re.findall(
                r'href="(?:\.\./)?([0-9A-Za-z][^"/]*@[^"/]+)/"', html))
            say(f"         {q!r}: {len(found)} 件")
            ids |= found
        ids = sorted(ids)
        if len(ids) > 60:
            say(f"       候補が {len(ids)} 件あるので新しい60件に絞ります")
            ids = ids[-60:]
        say(f"       ミラー上の候補: {len(ids)} 件")

        same_subject, same_diff = [], None
        for i, msgid in enumerate(ids, 1):
            if i % 10 == 0 or i == len(ids):
                say(f"       照合中 {i}/{len(ids)} ...")
            try:
                raw = self.fetch(f"{self.mirror}/{msgid}/raw")
            except Exception as exc:
                say(f"         (skip) {msgid}: {exc}")
                continue
            r_subject = ""
            for line in raw.splitlines():
                if line.startswith("Subject:"):
                    r_subject = line.split(":", 1)[1].strip()
                    break
            bare = re.sub(r"\s+", " ",
                          re.sub(r"^(\[[^\]]*\]\s*)+", "", r_subject)).strip()
            if bare == words:
                same_subject.append(msgid)
            if local and self.diff_digest(raw) == local:
                same_diff = msgid
        say("\r       " + " " * 30 + "\r", end="")
        return same_subject, same_diff


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_series(job, s):
    name = s["name"]
    branch = s["branch"]
    base = job.resolve_base(branch)
    out_branch = s.get("out_branch", f"send-{name}")
    pdir = os.path.join(job.workdir, name)
    reroll = str(s.get("reroll", "") or "")
    reply_to = s.get("in_reply_to")

    say(f"\n########## {name} ({branch}) base={base[:12]} ##########")

    t = head(f"{name}: 送信用ブランチと Signed-off-by")
    job.git(["switch", "-q", "master"], check=False)
    job.git(["branch", "-D", out_branch], check=False)
    job.git(["switch", "-q", "-c", out_branch, branch])
    job.git(["rebase", "--exec", "git commit --amend --no-edit -s", base])
    commit = subprocess.run(["git", "log", "-1", "--format=%h %s"],
                            cwd=job.repo, env=job.env, capture_output=True,
                            text=True).stdout.strip()
    ok(f"{out_branch} / {commit}", t)

    t = head(f"{name}: format-patch → {pdir}")
    subprocess.run(["rm", "-rf", pdir], env=job.env)
    cmd = ["format-patch"] + (["-v", reroll] if reroll else [])
    cmd += [f"--base={base}", "-o", pdir, base]
    job.git(cmd)
    patches = sorted(glob.glob(f"{pdir}/*.patch"))
    if not patches:
        raise Fatal(f"{pdir} が空です")
    ok(f"{len(patches)} 枚: {', '.join(os.path.basename(p) for p in patches)}", t)

    t = head(f"{name}: checkpatch --strict と git am")
    cp = subprocess.run(["perl", "scripts/checkpatch.pl", "--strict"] + patches,
                        cwd=job.repo, env=job.env, capture_output=True, text=True)
    for line in (cp.stdout + cp.stderr).splitlines():
        if "total:" in line or "ready for submission" in line:
            say(f"         {line}")
    log_block(f"--- checkpatch rc={cp.returncode}\n{cp.stdout}{cp.stderr}")
    if "0 errors, 0 warnings, 0 checks" not in cp.stdout + cp.stderr:
        raise Fatal("checkpatch")
    wt = os.path.join(os.path.dirname(job.repo), "wt-verify")
    job.git(["worktree", "remove", "--force", wt], check=False)
    job.git(["worktree", "prune"], check=False)
    job.git(["worktree", "add", "-q", "--detach", wt, base])
    am = subprocess.run(["git", "-C", wt, "am"] + patches, env=job.env,
                        capture_output=True, text=True)
    log_block(f"--- git am rc={am.returncode}\n{am.stdout}{am.stderr}")
    job.git(["worktree", "remove", "--force", wt], check=False)
    if am.returncode != 0:
        raise Fatal("git am\n" + (am.stdout + am.stderr)[-600:])
    ok("checkpatch 0/0/0、am クリーン", t)

    t = head(f"{name}: 既に送っていないか（{job.list} を照合）")
    with open(patches[0], encoding="utf-8") as fh:
        subject = re.sub(r"\s+", " ",
                         (email.message_from_file(fh)["Subject"] or "")).strip()
    no_tags = re.sub(r"^(\[[^\]]*\]\s*)+", "", subject)
    say(f"       件名: {no_tags}")
    same_subject, same_diff = job.check_duplicate(patches[0], subject, job.cc)
    if same_diff:
        if job.pending_only:
            say(f"       既に送信済み <{same_diff}> なのでスキップします")
            SKIPPED.append(name)
            return
        raise Fatal(f"同じ diff が既に投稿済み: <{same_diff}>")
    if same_subject:
        say("       同じ件名の既存投稿: "
              + ", ".join(f"<{m}>" for m in same_subject))
        if not reply_to:
            raise Fatal("同じ件名が既にあるのに返信先が無い（v2 として出すべき？）")
        say("       → 返信として送るので問題なし")
    else:
        say("       同じ件名の投稿なし")
    ok("重複なし", t)

    t = head(f"{name}: {'送信' if job.send else 'dry-run（送らない）'}"
             + ("" if not job.send else "  ← SMTP で1〜2分かかることがあります"))
    cmd = ["send-email", "--confirm=never"]
    for a in job.to:
        cmd.append(f"--to={a}")
    for a in job.cc:
        cmd.append(f"--cc={a}")
    if not job.send:
        cmd.append("--dry-run")
    if reply_to:
        cmd.append(f"--in-reply-to={reply_to}")
    cmd += patches
    se = subprocess.run(["git"] + cmd, cwd=job.repo, env=job.env,
                        capture_output=True, text=True)
    for line in (se.stdout + se.stderr).splitlines():
        if line.startswith(("Subject:", "In-Reply-To:", "Result:")) or "RCPT TO" in line:
            say(f"         {line}")
    log_block(f"--- send-email rc={se.returncode}\n{se.stdout}{se.stderr}")
    if se.returncode != 0:
        raise Fatal("git send-email\n" + (se.stdout + se.stderr)[-800:])
    ok(f"{name} {'送信' if job.send else 'dry-run'}", t)


def main():
    global TOTAL
    ap = argparse.ArgumentParser(description="カーネルパッチの送出（既定は dry-run）")
    ap.add_argument("series", nargs="*", help="送る系列名（省略時は設定の全部）")
    ap.add_argument("--config", default=CONFIG_DEFAULT)
    ap.add_argument("--base", help="base コミット（既定 auto=merge-base）")
    ap.add_argument("--list", help="宛先メーリングリスト")
    ap.add_argument("--to", action="append", help="To: を追加（複数可）")
    ap.add_argument("--cc", action="append", help="Cc: を追加（複数可）")
    ap.add_argument("--mirror", help="重複確認に使う lore ミラー")
    ap.add_argument("--workdir", help="パッチの出力先（既定 /tmp/kpatch）")
    ap.add_argument("--send", action="store_true", help="実際に送信する")
    ap.add_argument("--pending", action="store_true",
                    help="まだ送っていない系列だけを対象にする")
    ap.add_argument("--keep", action="store_true", help="作業ブランチを残す")
    ap.add_argument("--list-series", action="store_true", help="設定の系列を表示")
    args = ap.parse_args()

    lock_file = open("/tmp/kpatch-send.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("別の kpatch-send が実行中です（/tmp/kpatch-send.lock）。"
                 "二重起動するとブランチとパッチが壊れるので止めました。")

    cfg = load_config(args.config)
    series = cfg["series"]
    if args.list_series:
        for s in series:
            say(f"{s['name']:20s} {s['branch']:32s} "
                  f"reroll={s.get('reroll', '')} reply={s.get('in_reply_to', '')}")
        return
    if args.series:
        series = [s for s in series if s["name"] in args.series]
        if not series:
            sys.exit("該当する系列名がありません（--list-series で確認）")

    job = Job(cfg, args)

    global LOG, LOG_PATH
    logdir = os.path.expanduser(cfg.get("logdir", os.path.join(TOOL_DIR, "logs")))
    os.makedirs(logdir, exist_ok=True)
    LOG_PATH = os.path.join(logdir, time.strftime("%Y%m%d-%H%M%S") + ".log")
    LOG = open(LOG_PATH, "a", encoding="utf-8")

    TOTAL = len(series) * 5
    action = "送信" if job.send else "dry-run（送らない）"
    say(f"=== {len(series)}系列 {action} / list={job.list} / "
        f"base={job.base} / {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} ===")
    say(f"    作業ディレクトリ: {job.workdir}")
    say(f"    ログ: {LOG_PATH}")

    try:
        for s in series:
            run_series(job, s)
    except Fatal as exc:
        subprocess.run(["git", "rebase", "--abort"], cwd=job.repo, env=job.env,
                       capture_output=True)
        subprocess.run(["git", "worktree", "remove", "--force",
                        os.path.join(os.path.dirname(job.repo), "wt-verify")],
                       cwd=job.repo, env=job.env, capture_output=True)
        say(f"\n       NG  {exc}")
        say("止めました。以降の系列は送っていません。")
        say(f"ログ: {LOG_PATH}")
        sys.exit(1)
    finally:
        subprocess.run(["git", "switch", "-q", "master"], cwd=job.repo,
                       env=job.env, capture_output=True)

    say(f"\n=== 完了（合計 {clock():.1f}s）===")
    if SKIPPED:
        say(f"送信済みのためスキップ: {', '.join(SKIPPED)}")
    say(f"ログ: {LOG_PATH}")
    if not job.send:
        say(f"問題なければ: {sys.argv[0]} --send " + " ".join(args.series))


if __name__ == "__main__":
    main()
