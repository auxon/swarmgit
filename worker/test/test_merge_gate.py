#!/usr/bin/env python3
"""Tests for the SwarmGit merge gate (stdlib only; async via asyncio).

A fake `http` seam serves canned MCP JSON-RPC responses; each test runs
run_merge_gate against a scripted fork and asserts on the report.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
import merge_gate  # noqa: E402


# ---------------------------------------------------------------- fake http

def make_fake(tools, call_handler):
    """Fake http(method, url, headers, timeout, body=None) serving canned
    MCP JSON-RPC. call_handler(name, arguments) -> (status, payload)
    where payload is a JSON-RPC dict or a raw body string."""
    async def fake_http(method, url, headers, timeout, body=None):
        assert method == "POST", method
        req = json.loads(body.decode("utf-8") if isinstance(body, bytes)
                         else body)
        rid = req.get("id")
        m = req.get("method")
        if m == "initialize":
            return {"status": 200,
                    "body": json.dumps(
                        {"jsonrpc": "2.0", "id": rid,
                         "result": {"protocolVersion": "2025-06-18",
                                    "serverInfo": {"name": "fork-mcp",
                                                   "version": "0.1"}}}),
                    "headers": {"content-type": "application/json"}}
        if m == "notifications/initialized":
            return {"status": 202, "body": "", "headers": {}}
        if m == "tools/list":
            return {"status": 200,
                    "body": json.dumps(
                        {"jsonrpc": "2.0", "id": rid,
                         "result": {"tools": tools}}),
                    "headers": {"content-type": "application/json"}}
        if m == "tools/call":
            name = req["params"]["name"]
            args = req["params"]["arguments"]
            status, payload = call_handler(name, args)
            body_out = payload if isinstance(payload, str) \
                else json.dumps(payload)
            return {"status": status, "body": body_out,
                    "headers": {"content-type": "application/json"}}
        raise AssertionError(f"unexpected rpc method {m!r}")
    return fake_http


def rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def rpc_error(rid, code, message, data=None):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message, "data": data}}


def tool(name, properties, required=(), description=""):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required)}}


STR = {"type": "string"}
INT = {"type": "integer"}


# ---------------------------------------------------------------- tests

async def test_leaky_fork_blocks():
    """Fork whose tool output contains a live sk-live secret -> block,
    critical/confirmed, secret redacted in evidence."""
    secret = "sk-live-4f2a9c1d8e7b6a5f"  # 16 chars after sk-live-
    tools = [tool("get_config", {"key": STR}, ("key",),
                  "returns service configuration")]

    def handler(name, args):
        assert name == "get_config"
        return 200, rpc_result(1, {"content": [
            {"type": "text",
             "text": json.dumps({"api_key": secret, "region": "us"})}]})

    report = await merge_gate.run_merge_gate(
        make_fake(tools, handler), "https://fork-preview.example/mcp")
    assert report["decision"] == "block", report["decision"]
    assert report["mode"] == "preview"
    crits = [f for f in report["findings"]
             if f["severity"] == "critical"
             and f["confidence"] == "confirmed"]
    assert crits, f"no critical/confirmed finding: {report['findings']}"
    assert any(f["pack"] == "p3_leak" for f in crits)
    blob = json.dumps(crits, ensure_ascii=False)
    assert secret not in blob, "live secret leaked into finding evidence"
    assert "sk-live-…6a5f" in blob, f"redacted form missing: {blob[:400]}"
    assert merge_gate.gate_blocks(report) is True
    assert report["calls"] > 0


async def test_clean_fork_passes():
    """Clean fork (list + calls all sane) -> pass, all four packs run."""
    tools = [tool("echo", {"text": STR}, ("text",), "echoes text"),
             tool("add", {"a": INT, "b": INT}, ("a", "b"), "adds")]

    def handler(name, args):
        if name == "echo":
            if not isinstance(args.get("text"), str):
                return 200, rpc_error(1, -32602, "invalid arguments")
            return 200, rpc_result(1, {"echo": args["text"]})
        if name == "add":
            a, b = args.get("a"), args.get("b")
            if (isinstance(a, bool) or isinstance(b, bool)
                    or not isinstance(a, int) or not isinstance(b, int)):
                return 200, rpc_error(1, -32602, "invalid arguments")
            return 200, rpc_result(1, {"sum": a + b})
        raise AssertionError(name)

    report = await merge_gate.run_merge_gate(
        make_fake(tools, handler), "https://fork-preview.example/mcp")
    assert report["decision"] == "pass", \
        json.dumps(report["findings"], indent=1)[:2000]
    assert report["mode"] == "preview"
    assert list(report["packs_run"]) == list(merge_gate.GATE_PACKS), \
        report["packs_run"]
    assert not any(f["severity"] == "critical"
                   for f in report["findings"])
    assert merge_gate.gate_blocks(report) is False
    assert 0 < report["calls"] <= 400


async def test_scan_diff_aws_key():
    """scan_diff on a diff containing an AWS-style secret key ->
    critical/confirmed finding, redacted evidence."""
    aws_key = "AKIAZZZZYYYYXXXXWWQQ"  # AKIA + 16 uppercase alnum
    diff = (
        "diff --git a/config.py b/config.py\n"
        "index 111..222 100644\n"
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1,2 +1,3 @@\n"
        " REGION = \"us-east-1\"\n"
        f"+AWS_ACCESS_KEY_ID = \"{aws_key}\"\n"
        " TIMEOUT = 30\n"
    )
    findings = merge_gate.scan_diff(diff)
    crits = [f for f in findings
             if f["severity"] == "critical"
             and f["confidence"] == "confirmed"]
    assert crits, f"no critical/confirmed finding: {findings}"
    assert any(f["pack"] == "p3_leak" for f in crits)
    blob = json.dumps(findings, ensure_ascii=False)
    assert aws_key not in blob, "live AWS key leaked into evidence"
    assert "AKIAZZZZ…WWQQ" in blob, f"redacted form missing: {blob[:400]}"
    # p0-style surface inventory note present
    assert any(f["pack"] == "p0_recon" and f["severity"] == "info"
               for f in findings)
    # diff-mode report built on these findings blocks too
    assert merge_gate.gate_blocks({"findings": findings}) is True


async def test_p1_500_is_major_not_blocking():
    """HTTP 500 + Python traceback on empty arg (P1) -> major/confirmed
    present, but decision stays pass (only critical blocks)."""
    tools = [tool("lookup", {"query": STR}, ("query",), "looks up")]

    def handler(name, args):
        assert name == "lookup"
        if args == {}:
            return 500, ("Traceback (most recent call last):\n"
                         '  File "app.py", line 42, in lookup\n'
                         "ValueError: empty args dict")
        return 200, rpc_result(1, {"result": "ok"})

    report = await merge_gate.run_merge_gate(
        make_fake(tools, handler), "https://fork-preview.example/mcp")
    majors = [f for f in report["findings"]
              if f["severity"] == "major"
              and f["confidence"] == "confirmed"]
    assert majors, f"no major/confirmed finding: {report['findings']}"
    assert any(f["pack"] == "p1_fuzz" for f in majors), \
        [ (f["pack"], f["title"]) for f in report["findings"] ]
    assert report["decision"] == "pass", report["decision"]
    assert merge_gate.gate_blocks(report) is False


async def test_gate_blocks_consistency():
    """gate_blocks agrees with the decisions above, plus edge cases."""
    assert merge_gate.gate_blocks({"findings": []}) is False
    assert merge_gate.gate_blocks(
        {"findings": [{"severity": "critical",
                       "confidence": "suspected"}]}) is False
    assert merge_gate.gate_blocks(
        {"findings": [{"severity": "critical",
                       "confidence": "confirmed"}]}) is True
    assert merge_gate.gate_blocks(
        {"findings": [{"severity": "major",
                       "confidence": "confirmed"}]}) is False
    assert merge_gate.gate_blocks({}) is False
    assert merge_gate.gate_blocks(None) is False


async def test_unreachable_target_falls_back_to_diff():
    """Unreachable preview URL -> diff-only mode, no invented findings."""
    async def dead_http(method, url, headers, timeout, body=None):
        raise ConnectionError("connection refused")

    diff = ("diff --git a/app.py b/app.py\n"
            "+print('hello')\n")
    report = await merge_gate.run_merge_gate(
        dead_http, "https://fork-preview.example/mcp", diff_text=diff)
    assert report["mode"] == "diff", report["mode"]
    assert report["calls"] == 0
    assert list(report["packs_run"]) == ["p0_recon", "p3_leak"]
    assert report["decision"] == "pass"  # clean diff -> no critical
    assert any("unreachable" in f["title"] for f in report["findings"])


async def test_strict_seam_without_body_falls_back_to_diff():
    """http seam with exactly (method, url, headers, timeout) and no body
    channel -> clean diff-only fallback (no crash)."""
    async def strict_http(method, url, headers, timeout):
        return {"status": 200, "body": "{}", "headers": {}}

    report = await merge_gate.run_merge_gate(
        strict_http, "https://fork-preview.example/mcp",
        diff_text="diff --git a/a.py b/a.py\n+x = 1\n")
    assert report["mode"] == "diff"
    assert report["decision"] == "pass"
    assert any("no request-body channel" in f["title"]
               for f in report["findings"])


async def test_rate_limit_stops_cleanly():
    """Repeated 429s -> run stops, recorded as info, not error."""
    tools = [tool("echo", {"text": STR}, ("text",))]

    async def limited_http(method, url, headers, timeout, body=None):
        req = json.loads(body.decode("utf-8"))
        m = req.get("method")
        if m == "initialize":
            return {"status": 200,
                    "body": json.dumps(
                        {"jsonrpc": "2.0", "id": req.get("id"),
                         "result": {"protocolVersion": "2025-06-18",
                                    "serverInfo": {}}}),
                    "headers": {}}
        if m == "notifications/initialized":
            return {"status": 202, "body": "", "headers": {}}
        if m == "tools/list":
            return {"status": 200,
                    "body": json.dumps(
                        {"jsonrpc": "2.0", "id": req.get("id"),
                         "result": {"tools": tools}}),
                    "headers": {"content-type": "application/json"}}
        assert m == "tools/call", m
        return {"status": 429, "body": "slow down", "headers": {}}

    report = await merge_gate.run_merge_gate(
        limited_http, "https://fork-preview.example/mcp", max_calls=400)
    assert report["mode"] == "preview"
    assert report["decision"] == "pass"  # no findings -> nothing blocks
    infos = [f for f in report["findings"] if f["severity"] == "info"]
    assert any("429" in f["title"] for f in infos), \
        [f["title"] for f in infos]
    assert report["calls"] < 400  # stopped early, budget not exhausted


# ---------------------------------------------------------------- runner

async def main():
    tests = [
        ("leaky fork blocks", test_leaky_fork_blocks),
        ("clean fork passes", test_clean_fork_passes),
        ("scan_diff AWS key", test_scan_diff_aws_key),
        ("P1 500 is major, not blocking", test_p1_500_is_major_not_blocking),
        ("gate_blocks consistency", test_gate_blocks_consistency),
        ("unreachable -> diff mode", test_unreachable_target_falls_back_to_diff),
        ("strict seam -> diff mode", test_strict_seam_without_body_falls_back_to_diff),
        ("rate limit stops cleanly", test_rate_limit_stops_cleanly),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            await fn()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001 - surface unexpected errors
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"== {passed} passed, {failed} failed ==")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
