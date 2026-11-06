#!/bin/bash
# arm64 docs 系列のパッチを、Signed-off-by 付きで作り直す。
# 実行するのは本人（署名は本人しか付けられない）。何度実行しても同じ結果になる
# （cover letter の Date だけは実行時刻で変わる）。
#
#   bash build.sh
#
# 前提: ~/GitHub/linux に rev-arm64-{silicon-errata-fix,perf-doc-example}。
# defconfig は今サイクル送らないので、生成もゲートも対象外（意図的に外している）。
set -euo pipefail

REPO="$HOME/GitHub/linux"
OUT="$HOME/opt/patch-kernel/outgoing/arm64-docs"
BRANCHES=(arm64-silicon-errata-fix arm64-perf-doc-example)
EXPECT=(2 1)
SOB="Signed-off-by: TAKANO Mitsuhiro <takano32@gmail.com>"

cd "$REPO"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- 0. 中断状態と前提の確認 ---------------------------------------------
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ] ||
   [ -f .git/MERGE_HEAD ] || [ -f .git/CHERRY_PICK_HEAD ] ||
   [ -d .git/sequencer ]; then
	die "rebase/merge/cherry-pick の途中。中断してからやり直す"
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
	die "追跡ファイルに未コミットの変更がある。'git status --short' を確認"
fi
[ "${#BRANCHES[@]}" = "${#EXPECT[@]}" ] ||
	die "BRANCHES と EXPECT の数が合っていない"
for b in "${BRANCHES[@]}"; do
	git rev-parse --verify -q "refs/heads/rev-$b" >/dev/null ||
		die "ブランチ rev-$b が無い"
done

# --- 1. base を今の origin/master に合わせる ------------------------------
say "== base を決める"
git fetch -q origin master ||
	die "git fetch に失敗した。古い base で送らないよう、ここで止める（SSH のソケットを確認）"
BASE=$(git rev-parse origin/master)
git switch -q master
[ "$(git rev-parse master)" = "$BASE" ] ||
	die "ローカル master が origin/master と違う。揃えてからやり直す"
say "   $BASE"

# --- 2. 各ブランチを載せ替え、コミットごとに署名する --------------------
i=0
for b in "${BRANCHES[@]}"; do
	say
	say "== $b"
	git switch -q "rev-$b"
	git rev-parse --verify -q "refs/heads/bak-rev-$b" >/dev/null ||
		git branch "bak-rev-$b" "rev-$b"   # 最初の版を残す
	# -s は同じ SoB を重複して足さない（実測済み）ので、毎回 rebase してよい
	git rebase -q --exec 'git commit --amend --no-edit -s' "$BASE" ||
		die "rebase に失敗。'git rebase --abort' で戻せる"
	n=$(git rev-list --count "$BASE"..HEAD)
	[ "$n" = "${EXPECT[$i]}" ] ||
		die "$b のコミット数が $n（期待 ${EXPECT[$i]}）。上流に取り込まれた可能性を確認"
	signed=0
	for c in $(git rev-list "$BASE"..HEAD); do
		if git log -1 --format=%B "$c" | grep -Fxq "$SOB"; then
			signed=$((signed + 1))
		else
			say "   $c に Signed-off-by が無い"
		fi
	done
	[ "$signed" = "$n" ] ||
		die "$b の Signed-off-by が $signed/$n コミット。署名されていないコミットがある"
	git log --format='   %h %s' "$BASE"..HEAD
	i=$((i + 1))
done

git switch -q master

# --- 3. パッチを生成して差し替える ---------------------------------------
say
say "== パッチを生成"
rm -rf "$OUT"/errata "$OUT"/doc   # 失敗しても古い版が「送れる形」で残らないように
rm -rf "$OUT"/.stage.* 2>/dev/null || true
STAGE=$(mktemp -d "$OUT/.stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE"/errata "$STAGE"/doc

git format-patch -q --cover-letter --base="$BASE" -o "$STAGE/errata" "$BASE"..rev-arm64-silicon-errata-fix
git format-patch -q --base="$BASE" -o "$STAGE/doc" "$BASE"..rev-arm64-perf-doc-example

python3 - "$STAGE" "$OUT" <<'PY'
import glob, sys
stage, out = sys.argv[1], sys.argv[2]
path = glob.glob(stage + "/errata/0000-cover-letter.patch")[0]
text = open(path).read()
if "*** BLURB HERE ***" not in text or "*** SUBJECT HERE ***" not in text:
    sys.exit("cover letter のプレースホルダが無い（format-patch の出力形式が違う）")
text = text.replace("*** SUBJECT HERE ***", "arm64: errata: Fix two wrong Kconfig names")
body = open(out + "/cover.txt").read().strip()
if not body:
    sys.exit("cover.txt が空")
text = text.replace("*** BLURB HERE ***\n", body + "\n")
if "*** BLURB HERE ***" in text:
    sys.exit("cover letter の本文を差し込めなかった")
open(path, "w").write(text)
print("   cover letter:", path)
PY

# --- 4. checkpatch をゲートにする ---------------------------------------
say
say "== checkpatch"
fail=0
want=0
for n in "${EXPECT[@]}"; do want=$((want + n)); done
count=0
for f in "$STAGE"/*/*.patch; do
	case "$f" in */0000-cover-letter.patch) continue ;; esac
	count=$((count + 1))
	log=$(mktemp)
	perl scripts/checkpatch.pl --strict "$f" >"$log" 2>&1 || true
	out=$(grep -E '^total' "$log" || true)
	printf '   %-58s %s\n' "$(basename "$f")" "${out:-（出力なし）}"
	case "$out" in
	"total: 0 errors, 0 warnings"*) ;;
	"total: 0 errors"*)
		{ grep -E '^(WARNING|CHECK)' "$log" | head -10 || true; } >&2
		;;
	*)
		fail=1
		{ grep -E '^(ERROR|WARNING|CHECK)' "$log" | head -20 || true; } >&2
		{ head -5 "$log" || true; } >&2
		;;
	esac
	rm -f "$log"
done
[ "$count" = "$want" ] || die "パッチが $count 本（期待 $want 本）。系列が欠けている"
[ "$fail" = 0 ] || die "checkpatch が 0 errors でない。送信しない"
grep -q "^base-commit: $BASE$" "$STAGE"/errata/0000-cover-letter.patch ||
	die "cover letter の base-commit が $BASE でない"

# --- 5. 差し替え ---------------------------------------------------------
for d in errata doc; do
	rm -rf "$OUT/$d.prev"
	if [ -d "$OUT/$d" ]; then
		mv "$OUT/$d" "$OUT/$d.prev"
	fi
	mv "$STAGE/$d" "$OUT/$d"
done
rm -rf "$STAGE"
trap - EXIT

say
say "== できあがり（前の版があれば *.prev に残した）"
ls -1 "$OUT"/errata/*.patch "$OUT"/doc/*.patch
say
say "次: ~/opt/patch-kernel/send-series.py arm64-errata   （dry-run）"
