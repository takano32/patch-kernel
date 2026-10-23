#!/usr/bin/python3
"""レビューへの返信メールを送る（git send-email と同じ SMTP 設定を使う）。

本文は標準入力から読む。

    reply-mail.py --in-reply-to "<msgid>" --to <list> [--cc ...] < body.txt

--dry-run を付けると、ヘッダだけ表示して送らない。
"""

import argparse
import email.utils
import os
import smtplib
import ssl
import subprocess
import sys
import time


def git_config(key, default=None):
    out = subprocess.run(["git", "config", "--get", key],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return default
    return out.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", action="append", required=True)
    ap.add_argument("--cc", action="append", default=[])
    ap.add_argument("--in-reply-to", required=True)
    ap.add_argument("--references", action="append", default=[])
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body-file")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.body_file:
        body = open(args.body_file, encoding="utf-8").read()
    else:
        body = sys.stdin.read()
    if not body.strip():
        sys.exit("本文が空です（標準入力か --body-file で渡してください）")

    real_home = os.environ.get("REAL_HOME") or os.path.expanduser("~")
    name = git_config("user.name", "TAKANO Mitsuhiro")
    addr = git_config("user.email", "takano32@gmail.com")
    server = git_config("sendemail.smtpserver", "smtp.gmail.com")
    port = int(git_config("sendemail.smtpserverport", "587"))
    user = git_config("sendemail.smtpuser", addr)
    enc = git_config("sendemail.smtpencryption", "tls")
    password = git_config("sendemail.smtppass")

    msgid = email.utils.make_msgid(domain=addr.split("@")[-1])
    refs = " ".join(args.references + [args.in_reply_to])
    headers = [
        f"From: {name} <{addr}>",
        f"To: {', '.join(args.to)}",
        f"Subject: {args.subject}",
        f"Date: {email.utils.formatdate(localtime=True)}",
        f"Message-ID: {msgid}",
        f"In-Reply-To: {args.in_reply_to}",
        f"References: {refs}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
        "Content-Transfer-Encoding: 8bit",
        "X-Mailer: reply-mail.py",
    ]
    if args.cc:
        headers.insert(2, f"Cc: {', '.join(args.cc)}")

    message = "\n".join(headers) + "\n\n" + body.rstrip() + "\n"
    rcpts = list(args.to) + list(args.cc)

    if args.dry_run:
        print(message)
        print(f"--- dry-run: {server}:{port} / {enc} / From <{addr}> / "
              f"{len(rcpts)} 宛先 / 送っていません ---")
        return

    if not password:
        sys.exit("SMTP パスワードが git config sendemail.smtppass にありません")

    ctx = ssl.create_default_context()
    if enc == "ssl":
        smtp = smtplib.SMTP_SSL(server, port, timeout=60, context=ctx)
    else:
        smtp = smtplib.SMTP(server, port, timeout=60)
        smtp.ehlo()
        if enc in ("tls", "starttls"):
            smtp.starttls(context=ctx)
            smtp.ehlo()
    smtp.login(user, password)
    refused = smtp.sendmail(addr, rcpts, message.encode("utf-8"))
    smtp.quit()
    print(f"sent Message-ID: {msgid}")
    if refused:
        print(f"refused: {refused}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
