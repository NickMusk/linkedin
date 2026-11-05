#!/usr/bin/env python3
"""One command for a day of the investor campaign.

Both campaigns need the same handful of steps every day, in an order that matters:
find out who accepted before writing to them, and never fire invitations and messages
as one burst, which is what drew a 422 out of LinkedIn on 8 September.

This runs them in that order, respects the rolling daily caps, and writes a log per
run so a batch that dies leaves evidence behind.

    python3 daily.py                      # show the plan, send nothing
    python3 daily.py --send               # do it
    python3 daily.py --send --force       # same, but invitations go out on a weekend too
"""

import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOGS = BASE_DIR / "logs"
LOCK = BASE_DIR / "daily.lock"
GAP_BETWEEN_STEPS = 15 * 60      # invitations and messages must not look like one burst

STEPS = [
    # The two scans come first and send nothing. They take people out of the queues:
    # anyone who answered, and anyone Nick was already talking to before we started.
    ("who replied or is already in conversation", ["invite_campaign.py", "scan"], False),
    ("same, for the contact cohort", ["network_campaign.py", "scan"], False),
    ("check who accepted", ["invite_campaign.py", "check"], False),
    # New leads arrive without provider ids. Capped per run, so a large intake spreads
    # over a few days instead of burning the daily profile read budget at once.
    ("look up new profiles", ["invite_campaign.py", "resolve"], False),
    ("touches to people who accepted", ["invite_campaign.py", "touch"], True),
    ("invitations", ["invite_campaign.py", "invite"], True),
    ("messages to existing contacts", ["network_campaign.py", "touch"], True),
    # Last, and it sends nothing: who is waiting on a human answer from Nick.
    ("waiting on you", ["inbox.py", "unanswered"], False),
    # Draft today's LinkedIn post (Nick's idea from linkedin_post_ideas.md, or
    # the market). Publishing stays manual: the review session in the menu bar.
    ("draft today's LinkedIn post", ["linkedin_post.py", "gen"], False),
]


def run(script_args, live, log):
    cmd = [sys.executable, *script_args] + (["--send"] if live else [])
    print(f"\n$ {' '.join(cmd[1:])}", flush=True)
    with open(log, "a") as fh:
        fh.write(f"\n$ {' '.join(cmd[1:])}\n")
        fh.flush()
        proc = subprocess.run(cmd, cwd=BASE_DIR, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    out = "\n".join(l for l in proc.stdout.splitlines()
                    if "NotOpenSSL" not in l and "warnings.warn" not in l)
    print(out, flush=True)
    with open(log, "a") as fh:
        fh.write(out + "\n")
    return proc.returncode == 0, out


def summary():
    c = json.loads((BASE_DIR / "campaign_state.json").read_text())
    n = json.loads((BASE_DIR / "network_state.json").read_text())
    invited = sum(1 for p in c.values() if p["invited_at"])
    accepted = sum(1 for p in c.values() if p["accepted_at"])
    retry = sum(1 for p in c.values() if p["last_error"] and not p["invited_at"])
    started = sum(1 for p in n.values() if p["touches"])
    finished = sum(1 for p in n.values() if len(p["touches"]) == 4)
    print("\n" + "=" * 60)
    print(f"CRM      invited {invited}/{len(c)}  accepted {accepted}  awaiting retry {retry}")
    print(f"network  started {started}/{len(n)}  finished the four touches {finished}")
    print("=" * 60)


def acquire_lock():
    """Refuse to run twice at once.

    Once this is on a schedule, a hand started run and the scheduled one can overlap,
    and two senders sharing the same state file would message people twice.
    """
    if LOCK.exists():
        pid = LOCK.read_text().strip()
        try:
            os.kill(int(pid), 0)                   # signal 0 only tests for existence
        except (ValueError, ProcessLookupError):
            print(f"clearing stale lock from pid {pid}")
        except PermissionError:
            pass                                   # someone else's process, still alive
        else:
            print(f"another run is in progress (pid {pid}). nothing to do.")
            return False
    LOCK.write_text(str(os.getpid()))
    return True


def main():
    live = "--send" in sys.argv
    LOGS.mkdir(exist_ok=True)
    # The lock covers dry runs too: the acceptance check writes state in both modes,
    # so a dry run alongside a live one races on the same file.
    if not acquire_lock():
        return
    try:
        _run(live)
    finally:
        if LOCK.exists():
            LOCK.unlink()


def _run(live):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    log = LOGS / f"{stamp}.log"

    print(f"{'LIVE' if live else 'DRY RUN'} | log: {log.relative_to(BASE_DIR)}")
    if live and datetime.date.today().weekday() >= 5:
        print("weekend. invitations will decline on their own; messages still go.")

    sending_steps = [s for s in STEPS if s[2]]
    force = "--force" in sys.argv          # invitations otherwise wait for Monday
    for n, (title, args, is_send) in enumerate(STEPS):
        print(f"\n### {title}")
        forced = force and args == ["invite_campaign.py", "invite"]
        run(args + (["--force"] if forced else []), live and is_send, log)
        # Space the outbound steps apart. A dry run has nothing to space.
        if live and is_send and s_remaining(sending_steps, args):
            print(f"\n... waiting {GAP_BETWEEN_STEPS // 60} min before the next batch",
                  flush=True)
            time.sleep(GAP_BETWEEN_STEPS)

    summary()
    if not live:
        print("\nnothing was sent. add --send to run it for real.")


def s_remaining(sending_steps, args):
    """True when another outbound step still follows this one."""
    idx = [s[1] for s in sending_steps].index(args)
    return idx < len(sending_steps) - 1


if __name__ == "__main__":
    main()
