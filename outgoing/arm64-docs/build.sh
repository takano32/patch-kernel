#!/bin/bash
# arm64 docs 系列のパッチを、Signed-off-by 付きで作り直す。
# 実行するのは本人（署名は本人しか付けられない）。
# 何度実行しても結果は同じになる。
set -euo pipefail

REPO="$HOME/GitHub/linux"
OUT="$HOME/opt/patch-kernel/outgoing/arm64-docs"
BASE="6628e7d4c6b25c8d18f1345e3a20d21829aae7e4"
BRANCHES=(arm64-silicon-errata-fix arm64-perf-doc-example arm64-defconfig-stale)

cd "$REPO"

if [ -n "$(git status --porcelain)" ]; then
	echo "作業ツリーが汚れている。中断する:" >&2
	git status --short >&2
	exit 1
fi

echo "== master を確認"
git switch -q master
git branch --show-current

for b in "${BRANCHES[@]}"; do
	echo
	echo "== $b に Signed-off-by を入れる"
	git switch -q "rev-$b"
	if git log --format=%B "origin/master..HEAD" | grep -q '^Signed-off-by: TAKANO Mitsuhiro'; then
		echo "   すでに入っている（変更なし）"
	else
		git rebase -q --exec 'git commit --amend --no-edit -s' origin/master
	fi
	git log --format='   %h %s' "origin/master..HEAD"
done

git switch -q master
echo
echo "== パッチを生成"
rm -rf "$OUT/errata" "$OUT/doc" "$OUT/defconfig"
mkdir -p "$OUT/errata" "$OUT/doc" "$OUT/defconfig"
git format-patch -q --cover-letter --base="$BASE" -o "$OUT/errata" origin/master..rev-arm64-silicon-errata-fix
git format-patch -q --base="$BASE" -o "$OUT/doc" origin/master..rev-arm64-perf-doc-example
git format-patch -q --base="$BASE" -o "$OUT/defconfig" origin/master..rev-arm64-defconfig-stale

python3 - "$OUT" <<'PY'
import glob, re, sys
out = sys.argv[1]
p = glob.glob(out + "/errata/0000-cover-letter.patch")[0]
s = open(p).read()
s = s.replace("*** SUBJECT HERE ***", "arm64: errata: Fix two wrong Kconfig names")
body = open(out + "/cover.txt").read()
s = re.sub(r"\*\*\* BLURB HERE \*\*\*\n", body.rstrip("\n") + "\n\n", s, count=1)
open(p, "w").write(s)
print("   cover letter:", p)
PY

echo
echo "== checkpatch"
for f in "$OUT"/errata/0001*.patch "$OUT"/errata/0002*.patch "$OUT"/doc/*.patch "$OUT"/defconfig/*.patch; do
	printf '%-58s ' "$(basename "$f")"
	perl scripts/checkpatch.pl --strict "$f" 2>&1 | grep -E '^total' || echo "NG"
done

echo
echo "== 生成物"
ls -1 "$OUT"/errata "$OUT"/doc "$OUT"/defconfig
