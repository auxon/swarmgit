#!/usr/bin/env python3
"""Acceptance tests for the SwarmGit demo task.

Run:  python3 acceptance_tests.py <base-url> [--only headers_present|limit_values_sane]

The <base-url> is the fork's preview URL, e.g. http://127.0.0.1:8765
(tests POST to <base-url>/mcp).

  headers_present  — every tool response carries X-RateLimit-Limit and
                     X-RateLimit-Remaining (including error responses).
  limit_values_sane — limit is a positive int; 0 <= remaining <= limit;
                     remaining decrements by exactly 1 per call; exhausting
                     the bucket yields HTTP 429 (headers still present).
Exit 0 iff the selected tests pass.
"""
import json
import sys
import urllib.request
import urllib.error

TIMEOUT = 10


def rpc(base, method, params=None, msg_id=1):
    body = json.dumps({"jsonrpc": "2.0", "id": msg_id,
                       "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/mcp", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def call_tool(base, name, args, msg_id=1):
    return rpc(base, "tools/call",
               {"name": name, "arguments": args}, msg_id)


def check_headers_present(base):
    """Every tool response (ok AND error) carries the rate-limit headers."""
    probes = [("add_note", {"text": "hello"}),
              ("list_notes", {}),
              ("delete_note", {"id": 999999}),
              ("no_such_tool", {})]
    for i, (name, args) in enumerate(probes):
        status, headers, _ = call_tool(base, name, args, msg_id=100 + i)
        for h in ("X-RateLimit-Limit", "X-RateLimit-Remaining"):
            if h not in headers and h.lower() not in \
                    {k.lower() for k in headers}:
                return False, f"{name}: missing {h} (HTTP {status})"
    return True, "all tool responses carry X-RateLimit-Limit/Remaining"


def check_limit_values_sane(base):
    """Header values are sane integers; bucket decrements; 429 on empty."""
    _, h1, _ = call_tool(base, "list_notes", {}, msg_id=201)
    hdrs = {k.lower(): v for k, v in h1.items()}
    try:
        limit = int(hdrs["x-ratelimit-limit"])
        rem1 = int(hdrs["x-ratelimit-remaining"])
    except (KeyError, ValueError):
        return False, "headers missing or not integers"
    if limit <= 0:
        return False, f"limit not positive: {limit}"
    if not (0 <= rem1 <= limit):
        return False, f"remaining out of range: {rem1} (limit {limit})"
    _, h2, _ = call_tool(base, "list_notes", {}, msg_id=202)
    rem2 = int({k.lower(): v for k, v in h2.items()}["x-ratelimit-remaining"])
    if rem2 != rem1 - 1:
        return False, f"remaining did not decrement by 1: {rem1} -> {rem2}"
    # Exhaust the bucket (skip if the agent chose a huge limit).
    if limit > 500:
        return True, (f"limit={limit}, remaining decrements; "
                      "exhaustion check skipped (limit > 500)")
    for i in range(rem2):
        call_tool(base, "list_notes", {}, msg_id=300 + i)
    status, h3, _ = call_tool(base, "list_notes", {}, msg_id=399)
    if status != 429:
        return False, f"expected 429 on exhausted bucket, got {status}"
    hdrs3 = {k.lower(): v for k, v in h3.items()}
    if "x-ratelimit-limit" not in hdrs3 or \
            "x-ratelimit-remaining" not in hdrs3:
        return False, "429 response missing rate-limit headers"
    return True, f"limit={limit}, decrements by 1, 429 on exhaustion"


TESTS = {"headers_present": check_headers_present,
         "limit_values_sane": check_limit_values_sane}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    base = sys.argv[1]
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    names = [only] if only else list(TESTS)
    failed = 0
    for name in names:
        try:
            ok, msg = TESTS[name](base)
        except Exception as e:  # noqa: BLE001 — test harness
            ok, msg = False, f"exception: {type(e).__name__}: {e}"
        print(("PASS " if ok else "FAIL ") + name + " — " + msg)
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
