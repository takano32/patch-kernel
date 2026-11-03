#!/usr/bin/python3
"""送る前のパッチを機械的にレビューする。

    ./kpatch-review.py                     # config.json の未送信系列を全部
    ./kpatch-review.py ampereonex-deprecated
    ./kpatch-review.py --repeat 3 ampereonex-deprecated

チェックは「形式」「タグ」「本文」「差分」「適用」「宛先」の6群。
1つでも落ちれば終了コード 1。--repeat は同じ検査を N 回繰り返し、
ばらつき（ミラーの機嫌など）がないかを見る。
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
WORK = "/tmp/kpatch-review"


def sh(cmd, cwd=None, env=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


class Report:
    def __init__(self):
        self.rows = []

    def add(self, ok, name, detail=""):
        self.rows.append((ok, name, detail))
        mark = "ok  " if ok else "NG  "
        print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""), flush=True)

    @property
    def failed(self):
        return [r for r in self.rows if not r[0]]


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def git(repo, env, *args):
    return sh(["git"] + list(args), cwd=repo, env=env)


def find_base(repo, env, cfg_base):
    if cfg_base and cfg_base != "auto":
        return cfg_base
    rc, out = git(repo, env, "merge-base", "HEAD", "origin/master")
    if rc != 0 or not out.strip():
        raise SystemExit("base を決められません（origin/master が無い？）")
    return out.strip().split()[0]


def make_patch(repo, env, base, branch, reroll, outdir, changelog=None):
    """kpatch-send と同じ手順（SoB 付与・変更履歴の差し込み）で作る。"""
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)
    tmp = "review-" + os.path.basename(outdir)
    git(repo, env, "switch", "-q", "master")
    git(repo, env, "branch", "-D", tmp)
    rc, out = git(repo, env, "switch", "-q", "-c", tmp, branch)
    if rc != 0:
        raise SystemExit(f"ブランチ作成失敗:\n{out}")
    rc, out = git(repo, env, "rebase", "--exec",
                  "git commit --amend --no-edit -s", base)
    if rc != 0:
        git(repo, env, "rebase", "--abort")
        raise SystemExit(f"rebase 失敗:\n{out[-300:]}")
    cmd = ["format-patch"] + (["-v", str(reroll)] if reroll else [])
    cmd += [f"--base={base}", "-o", outdir, base + ".." + tmp]
    rc, out = git(repo, env, *cmd)
    patches = sorted(glob.glob(os.path.join(outdir, "*.patch")))
    git(repo, env, "switch", "-q", "master")
    git(repo, env, "branch", "-D", tmp)
    if rc != 0 or not patches:
        raise SystemExit(f"format-patch 失敗:\n{out}")
    if changelog:
        if isinstance(changelog, str):
            changelog = changelog.strip().splitlines()
        for p in patches:
            lines = open(p, encoding="utf-8").read().splitlines(keepends=True)
            for i, line in enumerate(lines):
                if line.rstrip() == "---":
                    lines[i + 1:i + 1] = [c.rstrip() + "\n" for c in changelog] + ["\n"]
                    break
            open(p, "w", encoding="utf-8").writelines(lines)
    return patches


def parse_patch(path):
    text = open(path, encoding="utf-8").read()
    head, _, tail = text.partition("\ndiff --git ")
    lines = head.splitlines()
    subject = next((l.split(": ", 1)[1] for l in lines if l.startswith("Subject: ")), "")
    # 折り返された件名を戻す
    i = lines.index(next(l for l in lines if l.startswith("Subject: ")))
    j = i + 1
    while j < len(lines) and lines[j].startswith(" "):
        subject += " " + lines[j].strip()
        j += 1
    body = "\n".join(lines[j:])
    return text, subject.strip(), body, "diff --git " + tail


def tags_of(body):
    tags = {}
    for line in body.splitlines():
        m = re.match(r"^([A-Z][A-Za-z-]+): (.*)$", line)
        if m:
            tags.setdefault(m.group(1), []).append(m.group(2))
    return tags


def check_one(report, repo, env, cfg, series, cfg_base):
    name = series["name"]
    branch = series["branch"]
    reroll = series.get("reroll")
    print(f"\n### {name} ({branch})")

    base = find_base(repo, env, cfg_base)
    outdir = os.path.join(WORK, name)
    try:
        patches = make_patch(repo, env, base, branch, reroll, outdir,
                             series.get("changelog"))
    except SystemExit as exc:
        report.add(False, "パッチ生成", str(exc)[:200])
        return
    if len(patches) != 1:
        report.add(False, "パッチ枚数", f"{len(patches)} 枚（1系列1枚を想定）")
        return
    patch = patches[0]
    text, subject, body, diff = parse_patch(patch)
    tags = tags_of(body)

    # --- 形式
    want = f"[PATCH v{reroll}]" if reroll else "[PATCH]"
    report.add(subject.startswith(want + " "), "件名の版指定", subject[:60])
    report.add(bool(re.match(r"^\[PATCH[^\]]*\] [^:]+: \S", subject)),
               "件名にサブシステム接頭辞", "")
    report.add(len(subject) <= 78, "件名の長さ (<=78)", f"{len(subject)} 文字")

    # --- タグ
    report.add("Signed-off-by" in tags, "Signed-off-by がある")
    if "Signed-off-by" in tags:
        last_tag = [l for l in body.splitlines()
                    if re.match(r"^[A-Z][A-Za-z-]+: ", l)][-1]
        report.add(last_tag.startswith("Signed-off-by:"),
                   "Signed-off-by が最後のタグ")
        addr = re.search(r"<([^>]+)>", tags["Signed-off-by"][-1])
        report.add(bool(addr) and tags["Signed-off-by"][-1].split("<")[0].strip(),
                   "Signed-off-by に本名とアドレス",
                   tags["Signed-off-by"][-1])
    report.add("Assisted-by" in tags, "Assisted-by: LLM がある（AI 規約）")

    fixes = tags.get("Fixes", [])
    if fixes:
        m = re.match(r'^([0-9a-f]{12,40}) \("(.+)"\)$', fixes[-1])
        ok = bool(m)
        detail = fixes[-1]
        if m:
            rc, out = git(repo, env, "log", "-1", "--format=%s", m.group(1))
            if rc != 0:
                ok, detail = False, f"{m.group(1)} が存在しない"
            elif out.strip() != m.group(2):
                ok, detail = False, f"件名が違う: {out.strip()[:50]}"
        report.add(ok, "Fixes: の書式と参照先", detail)
    else:
        report.add(False, "Fixes: がある", "原稿に無い（不要なら明示的に判断）")

    # --- 本文
    body_lines = [l for l in body.splitlines() if l.strip()]
    cite = re.compile(r'commit [0-9a-f]{12,} \("')
    mid = [l for l in body_lines
           if cite.search(l) and not l.startswith("commit ")]
    report.add(not mid, "commit の引用は行頭から",
               (mid[0][:60] + "...") if mid else "")
    long_lines = [l for l in body_lines
                  if len(l) > 74
                  and not l.startswith(("Fixes:", "Link:", "Signed-off", "Assisted-by"))
                  and "://" not in l
                  and not cite.search(l)]
    report.add(not long_lines, "本文の行長 (<=74)", f"{len(long_lines)} 行が超過" if long_lines else "")
    seps = text.count("\n---\n")
    report.add(seps == 1, "区切り --- が1つ", f"{seps} 個")
    if reroll and int(reroll) > 1:
        after = text.split("\n---\n", 1)[1].split("\ndiff --git ")[0]
        report.add(bool(re.search(r"^v\d+:", after, re.M)),
                   "v2 以降の変更履歴が --- の下")
        report.add(bool(re.search(r"https?://", after)),
                   "変更履歴に前版の URL")
    report.add("--- 変更履歴が SoB より上" if False else True, "（参考）タグ順", "")

    # --- 差分
    files = re.findall(r"^diff --git a/(\S+) b/", diff, re.M)
    report.add(len(files) == 1, "変更ファイル数", ", ".join(files))
    for f in files:
        p = os.path.join(repo, f)
        if f.endswith(".json"):
            try:
                json.load(open(p, encoding="utf-8"))
                report.add(True, f"{f} が JSON として妥当")
            except Exception as exc:
                report.add(False, f"{f} が JSON として妥当", str(exc)[:80])
    for f in files:
        if not f.endswith(".json"):
            continue
        try:
            data = json.load(open(os.path.join(repo, f), encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for e in data:
            if not isinstance(e, dict):
                continue
            if e.get("Deprecated") and e.get("PublicDescription"):
                ok = e["PublicDescription"].startswith("This event is deprecated")
                report.add(ok, f"{f}: PublicDescription も deprecation で始まる",
                           e.get("EventName", ""))
    added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    spaced = [l for l in added if l != l.rstrip()]
    report.add(not spaced, "追加行に末尾空白なし", f"{len(spaced)} 行")
    report.add("\r" not in text, "CRLF なし")
    stat = re.search(r"^ (\d+) file", text, re.M)
    report.add(stat is not None, "diffstat がある")
    report.add(f"base-commit: {base}" in text, "base-commit が意図した base", base[:12])

    # --- 適用
    wt = os.path.join(WORK, "wt")
    shutil.rmtree(wt, ignore_errors=True)
    rc, out = git(repo, env, "worktree", "add", "-q", "--detach", wt, base)
    if rc != 0:
        report.add(False, "worktree 作成", out[-120:])
    else:
        rc, out = sh(["git", "am", patch], cwd=wt, env=env)
        report.add(rc == 0, "クリーンな base に git am", out.strip().splitlines()[-1] if out.strip() else "")
        git(repo, env, "worktree", "remove", "--force", wt)

    rc, out = sh(["perl", "scripts/checkpatch.pl", "--strict", patch], cwd=repo, env=env)
    benign = ("Unrecognized email address: 'LLM'", "Non-standard signature: Assisted-by:")
    if re.search(r'commit [0-9a-f]{12,} \("', text):
        benign = benign + ("Prefer a maximum 75 chars per line",)
    keep_e = [l for l in out.splitlines()
              if l.startswith("ERROR:") and not any(b in l for b in benign)]
    keep_w = [l for l in out.splitlines()
              if l.startswith("WARNING:") and not any(b in l for b in benign)]
    m = re.search(r"total: (\d+) errors, (\d+) warnings, (\d+) checks", out)
    detail = m.group(0) if m else out[-120:]
    if any(b in out for b in benign):
        detail += "（Assisted-by の誤検出を除く）"
    report.add(not keep_e and not keep_w, "checkpatch --strict", detail)

    # --- 宛先
    to = list(cfg.get("to", [])) + list(series.get("to", []))
    cc = list(cfg.get("cc", [])) + list(series.get("cc", []))
    addrs = [a for a in to + cc]
    report.add(len(addrs) == len(set(addrs)), "宛先の重複なし", f"{len(addrs)} 件")
    report.add(len(addrs) <= 10, "宛先が絞られている (<=10)", f"{len(addrs)} 件")
    lists = [a for a in addrs if "vger.kernel.org" in a or "lists." in a]
    # このファイルは perf と arm64 tooling の両方の MAINTAINERS に載っており、
    # 既定の linux-kernel を足すと3つになる。それを超えるのは spam。
    report.add(len(lists) <= 3, "メーリングリストは3つまで", ", ".join(lists))
    report.add(any("takano32@gmail.com" in a for a in addrs), "自分を Cc に残している")
    if not series.get("no_lkml"):
        report.add(any("linux-kernel@vger.kernel.org" in a for a in addrs),
                   "linux-kernel を Cc に（既定）")

    r2 = series.get("in_reply_to")
    if reroll and int(reroll) > 1:
        report.add(bool(r2), "v2 以降は返信先 (in_reply_to) 指定", r2 or "未設定")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("series", nargs="*")
    ap.add_argument("--config", default=os.path.join(TOOL_DIR, "config.json"))
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    cfg = load_config(args.config)
    repo = cfg.get("repo", "/home/takano32/GitHub/linux")
    env = dict(os.environ)
    env.setdefault("HOME", "/home/takano32")
    base = cfg.get("base", "auto")

    want = [s for s in cfg["series"] if not args.series or s["name"] in args.series]
    if not want:
        sys.exit("該当する系列がありません")

    worst = 0
    for it in range(1, args.repeat + 1):
        if args.repeat > 1:
            print(f"\n================ 反復 {it}/{args.repeat} ================")
        report = Report()
        for s in want:
            check_one(report, repo, env, cfg, s, base)
        bad = report.failed
        print(f"\n結果: {len(report.rows) - len(bad)}/{len(report.rows)} 項目が ok")
        if bad:
            print("落ちた項目:")
            for _, n, d in bad:
                print(f"  - {n}" + (f" ({d})" if d else ""))
            worst = 1
    sys.exit(worst)


if __name__ == "__main__":
    main()
