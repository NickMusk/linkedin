#!/usr/bin/env python3
"""
Shows approved Twitter replies one by one with delays.
Open the tweet URL, paste the reply, then press Enter to continue.
"""
import os
import re
import sys
import time
import random
from report import find_latest_session

DELAY_MIN = 120
DELAY_MAX = 360


def parse_approved_replies(directory):
    path = os.path.join(directory, "replies.md")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        content = f.read()
    approved = []
    for block in content.split("\n---\n"):
        if "**STATUS:** approved" not in block:
            continue
        url_m = re.search(r"\*\*URL:\*\* (.+)", block)
        author_m = re.search(r"^## \d+\. (@\S+)", block, re.MULTILINE)
        reply_m = re.search(r"```\n([\s\S]+?)\n```", block)
        tweet_m = re.search(r"> (.+)", block)
        if url_m and reply_m:
            approved.append({
                "url": url_m.group(1).strip(),
                "author": author_m.group(1).strip() if author_m else "",
                "tweet": tweet_m.group(1).strip() if tweet_m else "",
                "reply": reply_m.group(1).strip(),
            })
    return approved


def main():
    d = find_latest_session()
    if not d:
        print("No session found.")
        sys.exit(1)

    approved = parse_approved_replies(d)
    if not approved:
        print("No approved replies found.")
        sys.exit(0)

    print(f"\n{len(approved)} approved replies to post.\n")
    print("For each: open the URL, paste the reply, press Enter when done.\n")
    input("Press Enter to start...")

    for i, item in enumerate(approved, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(approved)}] {item['author']}")
        print(f"URL: {item['url']}")
        print(f"\nTweet: {item['tweet'][:120]}")
        print(f"\nREPLY TO PASTE:\n{item['reply']}")
        print(f"{'='*60}")

        input("\nPress Enter after posting...")

        if i < len(approved):
            delay = random.randint(DELAY_MIN, DELAY_MAX)
            print(f"Waiting {delay}s before next...")
            time.sleep(delay)

    print(f"\nDone! {len(approved)} replies posted.")


if __name__ == "__main__":
    main()
