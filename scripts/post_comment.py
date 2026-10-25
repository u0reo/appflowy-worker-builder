#!/usr/bin/env python3
# analyze 結果 (summary.md) を commit comment として POST する。
# GitHub Actions 外からは実行しない (GITHUB_TOKEN / GITHUB_REPOSITORY 必須)。
import json, os, sys, time, urllib.request

sha, summary = sys.argv[1], sys.argv[2]
body = json.dumps({
    "body": "## jem16k analysis @ " + time.strftime("%F %T") + " (run "
            + os.environ.get("GITHUB_RUN_ID", "?") + ")\n\n"
            + open(summary, encoding="utf-8").read()[:60000]
}).encode()
url = ("https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"]
       + "/commits/" + sha + "/comments")
req = urllib.request.Request(url, data=body, method="POST", headers={
    "Authorization": "Bearer " + os.environ["GH_TOKEN"],
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req)
    print("commit comment posted:", r.status)
except urllib.error.HTTPError as e:
    print("::warning::commit comment 送信失敗 HTTP", e.code,
          e.read()[:500].decode("utf-8", "replace"))
except Exception as e:
    print("::warning::commit comment 送信失敗:", e)
