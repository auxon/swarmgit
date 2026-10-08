#!/usr/bin/env python3
"""Pull SwarmGit releases and sign them with the local bsv-walletd.

Run this on the machine where the daemon is unlocked. It dials out to
the worker, then asks loopback walletd to sign. Nothing inbound is opened.

  export SWARMSGIT_URL=https://entangleit.com/swarmgit
  export SWARMSGIT_WALLETD_TOKEN=...   # same secret as the worker
  python3 walletd_release.py
"""
import json
import os
import ssl
import urllib.request

SWARM = os.environ.get("SWARMSGIT_URL", "https://entangleit.com/swarmgit").rstrip("/")
TOKEN = os.environ.get("SWARMSGIT_WALLETD_TOKEN", "")
DAEMON = os.environ.get("BSV_WALLETD_URL", "https://127.0.0.1:2121").rstrip("/")


def _req(url, method="GET", body=None, bearer=""):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json",
               "User-Agent": "SwarmGit-release/1.0"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=30) as res:
        return json.loads(res.read().decode() or "{}")


def main():
    if not TOKEN:
        raise SystemExit("set SWARMSGIT_WALLETD_TOKEN")
    pending = _req(SWARM + "/walletd/pending", bearer=TOKEN).get("pending") or []
    if not pending:
        print("no pending releases")
        return
    for item in pending:
        signed = _req(DAEMON, "POST", {
            "jsonrpc": "2.0", "id": 1, "method": "send",
            "params": {"to": item["to"], "sats": item["sats"],
                       "label": item.get("label") or "swarmgit release"},
        })
        result = signed.get("result") or signed
        txid = result.get("txid") or ""
        if len(txid) != 64:
            print("daemon refused", item["task_id"], signed)
            continue
        ack = _req(SWARM + "/walletd/release", "POST",
                   {"task_id": item["task_id"], "txid": txid}, bearer=TOKEN)
        print("released", item["task_id"], txid, ack.get("ok"))


if __name__ == "__main__":
    main()
