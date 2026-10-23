# patch-kernel

カーネルパッチをメールで送るための小さな道具一式。AI は作成・検証・監視まで、
送信（`git send-email`）は本人が実行する。

## 置き場所

    /opt/patch-kernel/          （または ~/opt/patch-kernel/）
      kpatch-send.py            送信ツール本体
      config.json               既定の設定（base・宛先・系列）
      examples/                 別サブシステム用の設定例
      logs/                     実行ごとのログ（1実行1ファイル）

設定とログは**スクリプトと同じディレクトリ**を見るので、どこに移しても動く。

## 使い方

    ./kpatch-send.py --list-series      # 設定されている系列を表示
    ./kpatch-send.py                    # 全系列を事前チェック + dry-run
    ./kpatch-send.py stat-v2            # 系列を指定して dry-run
    ./kpatch-send.py --send             # 本送信（実行は本人）
    ./kpatch-send.py --config examples/arm64-docs.json --send

`--base`（既定 `auto` = `origin/master` との merge-base）、`--to` / `--cc` /
`--list` / `--mirror` / `--workdir` は設定を一時的に上書きする。

## 1系列ごとにやること

1. `send-<name>` ブランチを作り直し、`git rebase --exec '... -s'` で
   `Signed-off-by` を付ける
2. `format-patch`（`reroll: 2` なら `-v 2`、つまり `[PATCH v2]`）
3. `checkpatch --strict` が `0 errors, 0 warnings, 0 checks` でなければ中止
4. クリーンな worktree に `git am` して適用確認（失敗なら中止）
5. lore ミラーで**同じ diff が既に出ていないか**を sha256 で照合（あれば中止）
6. `git send-email`（`--send` のときだけ実送信。確認プロンプトは出さない）

## 設定（config.json）

    {
      "repo": "/home/takano32/GitHub/linux",
      "base": "auto",
      "list": "linux-perf-users@vger.kernel.org",
      "mirror": "https://lore-kernel.gnuweeb.org/linux-perf-users",
      "to": ["linux-perf-users@vger.kernel.org",
             "Arnaldo Carvalho de Melo <acme@kernel.org>"],
      "cc": ["Namhyung Kim <namhyung@kernel.org>",
             "Ian Rogers <irogers@google.com>",
             "TAKANO Mitsuhiro <takano32@gmail.com>"],
      "series": [
        {"name": "stat-v2", "branch": "perf-stat-tool-enable",
         "reroll": 2, "in_reply_to": "<v1 の Message-ID>"},
        {"name": "group", "branch": "perf-stat-tool-group-enable"}
      ]
    }

- `base: auto` は `git merge-base <branch> origin/master`
- `reroll`: v2 以降は 2, 3, ...
- `in_reply_to`: 既存スレッドへの返信にする場合だけ書く
- `logdir` / `workdir` を書けば上書きできる（既定は `./logs` と `/tmp/kpatch`）

## 安全装置

- `/tmp/kpatch-send.lock` で**二重起動を禁止**（同時実行はブランチとパッチを壊す）
- パッチの出力先は実行ごとに固有（`/tmp/kpatch/<日時>-<pid>/`）
- 途中で失敗したら `git rebase --abort` と worktree 掃除をしてから終了
- ログには各コマンドの生出力と rc を全部書く

## 送る前に人間がやること

- `--send` を実行するのは本人だけ（AI は投稿してはいけない。DCO の証明も本人）
- 送信後は `Result: 250` を確認し、lore ミラーで Message-ID を控える
