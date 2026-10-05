#!/usr/bin/env python3
"""Export staged SwarmGit why-graph entries to the agent-memory layer.

The worker stages why-graph entries in D1 (why_entries) but never posts
to any board itself. The operator runs this AFTER a merge:

  python3 tools/export_why.py --report merge_report.json [--live]

--report: JSON file shaped like get_merge_report's merges[] entry
          (needs "why_entries": [...] with id/kind/agent/text/topic/refs).
Default is DRY-RUN: prints what would be posted. --live shells to
`~/workspace/skills/agent-memory/bin/memory remember --visibility private`.

Board writes stay an explicit human step. Never run --live from a cron.
"""
import argparse
import json
import os
import subprocess
import sys

MEMORY_BIN = os.path.expanduser(
    "~/workspace/skills/agent-memory/bin/memory")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True,
                    help="merge report JSON (get_merge_report output file)")
    ap.add_argument("--live", action="store_true",
                    help="actually post (default: dry-run print only)")
    args = ap.parse_args()

    with open(args.report) as f:
        report = json.load(f)
    merges = report.get("merges", [report] if "why_entries" in report else [])
    staged = [e for m in merges for e in m.get("why_entries", [])]
    if not staged:
        print("no staged why entries in report; nothing to export")
        return 1
    for e in staged:
        text = e.get("text", "")
        topic = e.get("topic", "swarmgit")
        if args.live:
            subprocess.run(
                [MEMORY_BIN, "remember", "--text", text,
                 "--visibility", "private", "--topic", topic, "--live"],
                check=True)
            print(f"posted [{e.get('kind')}] #{topic}")
        else:
            print(f"[dry-run] remember --visibility private"
                  f" --topic {topic}\n  {text[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
