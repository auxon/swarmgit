#!/usr/bin/env python3
"""SwarmGit merge gate — vendored SAFE PreFlight probe packs.

The winning fork of a SwarmGit task merges only if it passes this gate:
the vendored safe packs (recon, input fuzzing, secret-leak hunting,
happy-path checks) run against the fork's preview deployment URL, which
must speak MCP (streamable HTTP). Any finding with severity "critical"
and confidence "confirmed" blocks the merge.

Vendored (copied, not imported — Worker bundles are per-directory) from:
  - ~/workspace/preflight/worker/src/probelib.py
      findings factory (slimmed to the gate schema), SEVERITY_ORDER /
      CONFIDENCES (+ the "suspected never exceeds major" hard rule),
      SECRET_PATTERNS, TRACE_MARKERS, scan_text_for_leaks,
      redact_secrets, arg_fuzz (+ _str_variants/_num_variants/_valid_seed/
      _deep), golden_args, BudgetTracker, excerpt
  - ~/workspace/preflight/worker/src/mcp_probe.py
      MCP_VERSION, CLIENT_INFO, parse_sse_stream, sse_json_payload
      (hand-rolled JSON-RPC over HTTP; streamable-HTTP only)
  - ~/workspace/preflight/worker/src/packs/p0_recon.py
      DESTRUCTIVE_HINTS, looks_destructive, tool-inventory info finding
  - ~/workspace/preflight/worker/src/packs/p1_fuzz.py
      CRASH_CODES, _is_invalid_input_variant, _has_trace, one finding per
      (tool, rule) dedupe, light pass for destructive-looking tools
  - ~/workspace/preflight/worker/src/packs/p3_leak.py
      golden + empty + empty-string-per-field probes; secret in output ->
      critical/confirmed; trace markers (no secret) -> major/confirmed;
      one finding per (tool, pattern)
  - ~/workspace/preflight/worker/src/packs/p6_ux.py
      golden-arg happy-path per non-destructive tool; RPC/transport
      failure -> major/confirmed; latency blowout rule
      (> max(2000ms, 10x run median)) -> minor/confirmed

Deliberately NOT vendored (see final report for one-line reasons):
  P4 injection heuristics (out of scope per gate spec), P2/P5/P7
  adversarial packs (not in GATE_PACKS), stdio + legacy-SSE transports,
  stdlib_http (no direct network — the injected http seam is the only
  I/O), GUARDRAIL_FOOTER, redact_auth, repro_call/audit_id/status/
  first_seen (slim finding schema per the gate interface).

HTTP SEAM NOTE — the gate interface declares
    http(method, url, headers, timeout) -> {"status","body","headers"}
with no request-body channel, but MCP JSON-RPC probing requires POST
bodies. merge_gate therefore passes the JSON-RPC request as a `body=`
keyword argument. At startup it inspects the callable's signature: if a
`body` parameter (or **kwargs) is accepted, preview probing proceeds;
otherwise preview mode is impossible through the seam and the gate
degrades to diff-only mode with an info finding (never an invented
finding, never a crash). Test fakes should accept
(method, url, headers, timeout, body=None).
"""

import inspect
import json
import re
import secrets
import time

GATE_PACKS = ("p0_recon", "p1_fuzz", "p3_leak", "p6_ux")

# ---------------------------------------------------------------- vendored
# from ~/workspace/preflight/worker/src/probelib.py (adapted: slim finding
# schema; redaction keeps first 8 / last 4 chars, e.g. sk-live-…4f2a)

SEVERITY_ORDER = ("info", "minor", "major", "critical")
CONFIDENCES = ("confirmed", "suspected")

SECRET_PATTERNS = [
    ("openai_key", re.compile(r"sk-(?:live-|test-)?[A-Za-z0-9]{16,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("pem_key",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY")),
    ("generic_secret",
     re.compile(r"(?i)(?:api[_-]?key|secret|passwd|password|token)"
                r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-+/=]{16,})[\"']?")),
]

# Strong, low-false-positive patterns -> critical/confirmed.
STRONG_SECRET_PATTERNS = frozenset(
    ("openai_key", "aws_access_key", "slack_token", "github_token",
     "pem_key"))

TRACE_MARKERS = ("traceback", "exception", "stack trace", "nullpointer",
                 "\tat ", "at java.", ".php on line", "fatal error")


def _new_finding(pack, title, severity, confidence, evidence,
                 fix_guidance):
    """Gate finding: {"id","pack","title","severity","confidence",
    "evidence","fix_guidance"}. Enforces the plan hard constraint:
    suspected findings never exceed major."""
    if severity not in SEVERITY_ORDER:
        raise ValueError(f"bad severity: {severity}")
    if confidence not in CONFIDENCES:
        raise ValueError(f"bad confidence: {confidence}")
    if confidence == "suspected" and severity == "critical":
        raise ValueError("suspected findings must never exceed major "
                         "(plan hard constraint)")
    return {
        "id": "f_" + secrets.token_hex(4),
        "pack": pack,
        "title": title,
        "severity": severity,
        "confidence": confidence,
        "evidence": evidence,        # {"response_excerpt": ..., "note": ...}
        "fix_guidance": fix_guidance,
    }


def _redact_value(val):
    """Redact a secret value for evidence: first 8 … last 4
    (e.g. sk-live-…4f2a). Never emits the live value."""
    v = (val or "").strip()
    if len(v) > 12:
        return v[:8] + "…" + v[-4:]
    return "…"


def scan_text_for_leaks(text):
    """Vendored from probelib (redaction widened to 8/4 per gate spec).

    Return (secrets_found, trace_found). secrets_found: list of
    {"pattern": name, "excerpt": ...} with the secret value redacted.
    """
    secrets_found = []
    for name, rx in SECRET_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        if name == "generic_secret" and m.lastindex:
            start, end = m.start(1), m.end(1)
        else:
            start, end = m.start(), m.end()
        red = _redact_value(text[start:end])
        wstart = max(0, start - 30)
        excerpt_txt = text[wstart:start] + red + text[end:end + 30]
        secrets_found.append({"pattern": name,
                              "excerpt": excerpt_txt.strip()})
    low = text.lower()
    trace_found = any(m in low for m in TRACE_MARKERS)
    return secrets_found, trace_found


def redact_secrets(text):
    """Replace every secret-pattern match with first8…last4."""
    if not text:
        return text
    for _, rx in SECRET_PATTERNS:
        text = rx.sub(lambda m: _redact_value(m.group(0)), text)
    return text


def _str_variants():
    return [
        ("empty", ""),
        ("long", "A" * 2000),
        ("unicode", "üñîçødé🎉"),
        ("sqli", "' OR '1'='1"),
        ("xss", "<script>alert(1)</script>"),
        ("nullstr", "null"),
        ("whitespace", "   "),
    ]


def _num_variants(is_int):
    v = [("negative", -1), ("zero", 0), ("huge", 999999999),
         ("wrongtype_str", "abc")]
    if is_int:
        v.append(("float", 1.5))
    return v


def arg_fuzz(schema, cap=24):
    """Yield (label, arguments) mutations for a tool inputSchema.

    Covers: per-type mutations, null for each field, missing each
    required field, wrong types, unknown extra params, deep nesting,
    huge arrays. Capped (default 24/tool) for request budgets.
    """
    if not isinstance(schema, dict):
        return
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    yielded = 0

    def emit(label, args):
        nonlocal yielded
        if yielded < cap:
            yielded += 1
            yield (label, args)

    for field, fschema in props.items():
        ftype = fschema.get("type", "string")
        variants = []
        if ftype == "string":
            variants = _str_variants()
        elif ftype in ("integer", "number"):
            variants = _num_variants(ftype == "integer")
        elif ftype == "boolean":
            variants = [("wrongtype_str", "yes")]
        elif ftype == "array":
            variants = [("empty_array", []),
                        ("huge_array", ["x"] * 1000)]
        elif ftype == "object":
            variants = [("empty_object", {}),
                        ("deep_nesting", _deep(10))]
        for label, val in variants:
            args = {f: _valid_seed((props.get(f) or {}).get("type",
                                                            "string"))
                    for f in props}
            args[field] = val
            yield from emit(f"{field} <- {label}", args)
        args = {f: _valid_seed((props.get(f) or {}).get("type", "string"))
                for f in props}
        args[field] = None
        yield from emit(f"{field} <- null", args)
        args = {f: _valid_seed((props.get(f) or {}).get("type", "string"))
                for f in props}
        args[field] = 12345 if ftype == "string" else "not-the-type"
        yield from emit(f"{field} <- wrongtype", args)

    for field in required:
        args = {f: _valid_seed((props.get(f) or {}).get("type", "string"))
                for f in props if f != field}
        yield from emit(f"missing required {field}", args)

    args = {f: _valid_seed((props.get(f) or {}).get("type", "string"))
            for f in props}
    args["__preflight_unknown"] = "x"
    yield from emit("unknown extra param", args)


def _valid_seed(ftype):
    if ftype == "string":
        return "seed"
    if ftype == "integer":
        return 1
    if ftype == "number":
        return 1.5
    if ftype == "boolean":
        return True
    if ftype == "array":
        return []
    if ftype == "object":
        return {}
    return "seed"


def _deep(depth):
    top = {"k0": {}}
    cur = top["k0"]
    for i in range(1, depth):
        cur[f"k{i}"] = {}
        cur = cur[f"k{i}"]
    return top


def golden_args(schema):
    """Plausible valid arguments synthesized from a schema (P6)."""
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") or {}
    out = {}
    for field, fschema in props.items():
        ftype = fschema.get("type", "string")
        if ftype == "string":
            out[field] = "u_123" if "user" in field or "_id" in field \
                else "hello"
        elif ftype == "integer":
            out[field] = 42
        elif ftype == "number":
            out[field] = 9.99
        elif ftype == "boolean":
            out[field] = True
        elif ftype == "array":
            out[field] = []
        elif ftype == "object":
            out[field] = {}
        else:
            out[field] = "hello"
    return out


class BudgetTracker:
    def __init__(self, max_requests):
        self.max_requests = max_requests
        self.spent = 0

    def take(self, n=1):
        if self.spent + n > self.max_requests:
            return False
        self.spent += n
        return True

    def left(self):
        return self.max_requests - self.spent


def excerpt(text, limit=2000):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


# ---------------------------------------------------------------- vendored
# from ~/workspace/preflight/worker/src/mcp_probe.py (streamable-HTTP only;
# no stdlib_http, no stdio, no legacy-SSE handshake)

MCP_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "SwarmGit-MergeGate", "version": "0.1.0"}
MAX_BODY = 8192
_MCP_TIMEOUT = 15
_RATE_LIMIT_STREAK = 3  # consecutive 429/503 -> stop cleanly


def parse_sse_stream(text):
    """Parse a text/event-stream body -> list of (event, data) tuples."""
    events = []
    text = text.replace("\r\n", "\n")
    for block in text.split("\n\n"):
        event, data_lines = "message", []
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line.strip() == "":
                continue
            else:
                data_lines.append(line)
        data = "\n".join(data_lines)
        if data or event != "message":
            events.append((event, data))
    return events


def sse_json_payload(text):
    """Extract the JSON-RPC payload from an SSE stream body: the last
    event whose data parses as a JSON object with 'jsonrpc'/'result'/
    'error'. Raises ValueError when none is found."""
    last = None
    for _event, data in parse_sse_stream(text):
        data = data.strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if isinstance(obj, dict) and (
                obj.get("jsonrpc") == "2.0" or "result" in obj
                or "error" in obj):
            last = obj
    if last is None:
        raise ValueError("no JSON-RPC payload found in SSE stream")
    return last


def _parse_rpc_body(body):
    body = (body or "").strip()
    if body.startswith("event:"):
        return sse_json_payload(body)
    try:
        return json.loads(body)
    except Exception as e:
        raise ValueError(f"response is not JSON: {e}")


# ---------------------------------------------------------------- vendored
# from ~/workspace/preflight/worker/src/packs/p0_recon.py

DESTRUCTIVE_HINTS = ("delete", "remove", "destroy", "drop", "reset",
                     "revoke", "purge", "wipe")


def looks_destructive(tool):
    name = tool.get("name", "").lower()
    ann = tool.get("annotations") or {}
    if ann.get("destructiveHint") is True:
        return True
    return any(h in name for h in DESTRUCTIVE_HINTS)


# ---------------------------------------------------------------- vendored
# from ~/workspace/preflight/worker/src/packs/p1_fuzz.py

CRASH_CODES = (-32603,)  # JSON-RPC internal error ~= HTTP 500


def _is_invalid_input_variant(label):
    return any(k in label for k in ("missing required", "<- wrongtype",
                                   "<- null"))


def _has_trace(text):
    low = (text or "").lower()
    return any(m in low for m in TRACE_MARKERS)


def _json(v):
    try:
        return json.dumps(v, default=str)
    except Exception:
        return str(v)


# ---------------------------------------------------------------- gate engine

class _StopProbing(Exception):
    """Control flow: budget spent or repeated 429/503. Always recorded
    as an info finding, never as an error."""


class _NoBodyChannel(Exception):
    """The http seam cannot carry a request body: MCP POSTs impossible."""


def _seam_accepts_body(http):
    """True when the injected http callable accepts a `body` kwarg (or
    **kwargs), so JSON-RPC POST bodies can be sent through it."""
    try:
        sig = inspect.signature(http)
    except (TypeError, ValueError):
        return True
    for p in sig.parameters.values():
        if p.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return "body" in sig.parameters


class _Run:
    """Per-run state: budget, call count, findings, MCP session."""

    def __init__(self, http, url, max_calls):
        self.http = http
        self.url = url
        self.budget = BudgetTracker(max_calls)
        self.max_calls = max_calls
        self.calls = 0
        self.findings = []
        self.packs_run = []
        self.session_id = None
        self._rpc_id = 0
        self._bad_streak = 0

    # -- http / rpc -------------------------------------------------
    def _next_id(self):
        self._rpc_id += 1
        return self._rpc_id

    async def _http(self, payload_bytes):
        if not self.budget.take():
            raise _StopProbing(
                f"call budget exhausted (max_calls={self.max_calls}); "
                "stopped early")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": "SwarmGit-MergeGate/0.1"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        t0 = time.monotonic()
        # NOTE: body= kwarg carries the JSON-RPC request (see module
        # docstring: the declared seam has no body channel of its own).
        resp = await self.http("POST", self.url, headers, _MCP_TIMEOUT,
                               body=payload_bytes)
        latency_ms = int((time.monotonic() - t0) * 1000)
        self.calls += 1
        status = resp.get("status", 0) or 0
        if status in (429, 503):
            self._bad_streak += 1
            if self._bad_streak >= _RATE_LIMIT_STREAK:
                raise _StopProbing(
                    f"repeated HTTP {status} "
                    f"(x{self._bad_streak}); stopping cleanly")
            return {"rate_limited": True, "http_status": status,
                    "latency_ms": latency_ms}
        self._bad_streak = 0
        sid = None
        for k, v in (resp.get("headers") or {}).items():
            if k.lower() == "mcp-session-id":
                sid = v
                break
        if sid and not self.session_id:
            self.session_id = sid
        return {"rate_limited": False, "http_status": status,
                "body": resp.get("body") or "", "latency_ms": latency_ms}

    async def _rpc(self, method, params):
        msg = {"jsonrpc": "2.0", "id": self._next_id(),
               "method": method, "params": params}
        r = await self._http(json.dumps(msg).encode("utf-8"))
        if r.get("rate_limited"):
            return {"ok": False,
                    "error": f"HTTP {r['http_status']} (rate limited)",
                    "latency_ms": r["latency_ms"], "retryable": True}
        try:
            obj = _parse_rpc_body(r["body"])
        except ValueError as e:
            return {"ok": False, "error": f"bad RPC envelope: {e}",
                    "http_status": r["http_status"],
                    "raw": (r["body"] or "").strip()[:MAX_BODY],
                    "latency_ms": r["latency_ms"]}
        if not isinstance(obj, dict):
            return {"ok": False,
                    "error": "RPC response JSON is not an object",
                    "http_status": r["http_status"],
                    "raw": (r["body"] or "").strip()[:MAX_BODY],
                    "latency_ms": r["latency_ms"]}
        return {"ok": True, "http_status": r["http_status"],
                "rpc_result": obj.get("result"),
                "rpc_error": obj.get("error"),
                "raw": (r["body"] or "").strip()[:MAX_BODY],
                "latency_ms": r["latency_ms"]}

    async def _notify(self, method, params):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            await self._http(json.dumps(msg).encode("utf-8"))
        except _StopProbing:
            raise
        except Exception:
            pass  # notifications are fire-and-forget

    async def call_tool(self, name, arguments):
        return await self._rpc("tools/call",
                               {"name": name,
                                "arguments": arguments or {}})

    # -- findings ---------------------------------------------------
    def add_info(self, pack, title, detail):
        self.findings.append(_new_finding(
            pack, title, "info", "confirmed",
            {"response_excerpt": excerpt(str(detail), 1000),
             "note": "gate telemetry, not a defect"}, ""))

    def report(self, mode):
        rep = {"decision": "pass",
               "findings": self.findings,
               "packs_run": self.packs_run,
               "mode": mode,
               "calls": self.calls}
        rep["decision"] = "block" if gate_blocks(rep) else "pass"
        return rep

    # -- packs ------------------------------------------------------
    async def pack_p0(self):
        """Recon: tools/list inventory + destructive flags."""
        r = await self._rpc("tools/list", {})
        tools = []
        if r.get("ok") and not r.get("rpc_error"):
            for t in (r.get("rpc_result") or {}).get("tools", []):
                if isinstance(t, dict) and "name" in t:
                    t["preflight_destructive"] = looks_destructive(t)
                    tools.append(t)
        else:
            self.add_info(
                "p0_recon", "tools/list failed; inventory unavailable",
                r.get("error") or _json(r.get("rpc_error")))
        n_destr = sum(1 for t in tools if t.get("preflight_destructive"))
        self.findings.append(_new_finding(
            "p0_recon",
            f"Scope: {len(tools)} tools inventoried, "
            f"{n_destr} flagged destructive",
            "info", "confirmed",
            {"response_excerpt":
             ", ".join(t["name"] for t in tools)[:2000],
             "note": "destructive-looking tools are fuzzed lightly "
                     "or skipped by later packs"},
            ""))
        self.packs_run.append("p0_recon")
        return tools

    async def pack_p1(self, tools):
        """Input fuzzing: schema-aware mutations per tool."""
        seen = set()  # (tool, rule)

        def add(tool, rule, title, severity, args, evidence, fix):
            key = (tool, rule)
            if key in seen:
                return
            seen.add(key)
            self.findings.append(_new_finding(
                "p1_fuzz", title, severity, "confirmed", evidence, fix))

        for tool in tools:
            name = tool["name"]
            schema = tool.get("inputSchema") or {}
            light = bool(tool.get("preflight_destructive"))
            for label, args in arg_fuzz(schema, cap=24):
                if light and not ("empty" in label
                                  or "missing required" in label):
                    continue
                res = await self.call_tool(name, args)
                if _is_crash(res):
                    blob = _summarize_error(res)
                    trace = _has_trace(blob)
                    add(name, "unhandled_exception",
                        f"{name}: unhandled exception on {label}",
                        "major", args,
                        {"response_excerpt":
                         excerpt(redact_secrets(blob), 1500),
                         "note": "traceback markers present in error "
                                 "payload" if trace else
                                 "HTTP 500 / JSON-RPC -32603 on fuzzed "
                                 "input; no traceback markers"},
                        "Validate inputs at the tool boundary; catch "
                        "exceptions and return a typed error (e.g. "
                        "INVALID_ARGUMENT) instead of an internal error.")
                elif not res.get("ok"):
                    # transport-level failure (or undecodable envelope on a
                    # non-500): not a finding by itself; reachability is
                    # noted by P0/P6.
                    continue
                elif (res.get("rpc_error") is None
                      and _is_invalid_input_variant(label)):
                    add(name, "accepts_invalid_input",
                        f"{name}: accepted invalid input ({label})",
                        "minor", args,
                        {"response_excerpt":
                         excerpt(res.get("raw") or "", 500),
                         "note": "server returned success for input that "
                                 "violates its own schema"},
                        "Validate arguments against the inputSchema and "
                        "reject invalid input with INVALID_ARGUMENT.")
        self.packs_run.append("p1_fuzz")

    async def pack_p3(self, tools):
        """Secret/PII-leak hunting over tool outputs and error payloads."""
        seen = set()  # (tool, pattern)
        for tool in tools:
            name = tool["name"]
            schema = tool.get("inputSchema") or {}
            probes = [("golden", golden_args(schema)), ("empty", {})]
            for field, fschema in (schema.get("properties") or {}).items():
                if fschema.get("type", "string") == "string":
                    a = golden_args(schema)
                    a[field] = ""
                    probes.append((f"empty-string {field}", a))
            for args_label, args in probes:
                res = await self.call_tool(name, args)
                if not res.get("ok") and res.get("http_status") != 500:
                    # transport-level failure: not a finding by itself.
                    # (HTTP 500 bodies are still scanned — error payloads
                    # are in scope for leak hunting.)
                    continue
                texts = []
                if res.get("rpc_result") is not None:
                    texts.append(_json(res["rpc_result"]))
                err = res.get("rpc_error") or {}
                if err:
                    texts.append(str(err.get("message", "")))
                    texts.append(_json(err.get("data")))
                texts.append(res.get("raw") or "")
                blob = "\n".join(t for t in texts if t)
                secrets_found, trace_found = scan_text_for_leaks(blob)
                for m in secrets_found:
                    key = (name, m["pattern"])
                    if key in seen:
                        continue
                    seen.add(key)
                    self.findings.append(_new_finding(
                        "p3_leak",
                        f"{name}: live secret in tool output "
                        f"({m['pattern']})",
                        "critical", "confirmed",
                        {"response_excerpt": m["excerpt"],
                         "note": "secret value redacted; pattern: "
                                 + m["pattern"]},
                        "Never return secrets in tool outputs or errors; "
                        "store them server-side and return opaque "
                        "references. Rotate any exposed credential "
                        "immediately."))
                if trace_found and not secrets_found:
                    key = (name, "trace")
                    if key in seen:
                        continue
                    seen.add(key)
                    self.findings.append(_new_finding(
                        "p3_leak",
                        f"{name}: stack-trace markers in response "
                        f"({args_label} args)",
                        "major", "confirmed",
                        {"response_excerpt":
                         excerpt(redact_secrets(blob), 800),
                         "note": "trace markers in tool output or error "
                                 "payload"},
                        "Sanitize error responses at the tool boundary; "
                        "log full traces server-side only."))
        self.packs_run.append("p3_leak")

    async def pack_p6(self, tools):
        """UX mystery-shop: golden (valid) call per non-destructive tool."""
        results = []  # (name, args, res)
        latencies = []
        for tool in tools:
            name = tool["name"]
            if tool.get("preflight_destructive"):
                continue  # never fire a destructive happy path unprompted
            args = golden_args(tool.get("inputSchema") or {})
            res = await self.call_tool(name, args)
            results.append((name, args, res))
            if res.get("ok"):
                latencies.append(res.get("latency_ms", 0))
        lat_sorted = sorted(latencies)
        median = lat_sorted[len(lat_sorted) // 2] if lat_sorted else 0
        blowout_at = max(2000, median * 10)
        for name, args, res in results:
            if not res.get("ok"):
                if res.get("retryable"):
                    continue  # single 429/503; streak logic stops the run
                self.findings.append(_new_finding(
                    "p6_ux",
                    f"{name}: happy-path call failed at transport level",
                    "major", "confirmed",
                    {"response_excerpt": excerpt(res.get("error", ""),
                                                500),
                     "note": "golden (valid) arguments; the tool should "
                             "succeed"},
                    "Fix the happy path first — if the valid call fails, "
                    "nothing else matters."))
                continue
            err = res.get("rpc_error")
            if err:
                self.findings.append(_new_finding(
                    "p6_ux",
                    f"{name}: happy-path call returned RPC error "
                    f"{err.get('code')}: "
                    f"{str(err.get('message', ''))[:80]}",
                    "major", "confirmed",
                    {"response_excerpt":
                     excerpt(str(err.get("message", "")), 500),
                     "note": "golden (valid) arguments"},
                    "Fix the happy path; add a golden-call smoke test "
                    "to CI."))
                continue
            lat = res.get("latency_ms", 0)
            if lat > blowout_at:
                self.findings.append(_new_finding(
                    "p6_ux",
                    f"{name}: latency blowout "
                    f"({lat}ms > {blowout_at}ms)",
                    "minor", "confirmed",
                    {"response_excerpt": f"latency_ms={lat}, "
                                        f"run median={median}",
                     "note": "blowout rule: > max(2000ms, 10x run median)"},
                    "Profile the slow path; consider caching or async "
                    "execution for expensive tools."))
        self.packs_run.append("p6_ux")


def _is_crash(res):
    """Vendored P1 crash rule, plus the gate-spec override: an HTTP 500
    on a fuzzed call is a confirmed unhandled exception — even when the
    500 body is not a valid JSON-RPC envelope."""
    if res.get("http_status") == 500:
        return True
    if not res.get("ok"):
        return False
    err = res.get("rpc_error") or {}
    return err.get("code") in CRASH_CODES


def _summarize_error(res):
    if res.get("http_status") == 500 and not res.get("rpc_error"):
        return res.get("raw") or "HTTP 500 with empty body"
    err = res.get("rpc_error") or {}
    return str(err.get("message", "")) + " | " + _json(err.get("data"))


# ---------------------------------------------------------------- public API

def gate_blocks(report):
    """True iff any finding has severity == "critical" AND
    confidence == "confirmed". Only that combination blocks a merge."""
    findings = (report or {}).get("findings") or []
    return any(f.get("severity") == "critical"
               and f.get("confidence") == "confirmed"
               for f in findings)


def scan_diff(diff_text):
    """P3 secret/PII patterns over a merge diff + P0-style surface
    inventory notes. A live-looking secret -> critical/confirmed
    (redacted in evidence). Returns findings in the gate schema."""
    findings = []
    text = diff_text or ""
    seen = set()  # one finding per pattern
    for name, rx in SECRET_PATTERNS:
        m = rx.search(text)
        if not m or name in seen:
            continue
        seen.add(name)
        if name == "generic_secret" and m.lastindex:
            start, end = m.start(1), m.end(1)
        else:
            start, end = m.start(), m.end()
        red = _redact_value(text[start:end])
        wstart = max(0, start - 40)
        excerpt_txt = text[wstart:start] + red + text[end:end + 40]
        severity = ("critical" if name in STRONG_SECRET_PATTERNS
                    else "major")
        findings.append(_new_finding(
            "p3_leak",
            f"diff: live secret pattern in merge diff ({name})",
            severity, "confirmed",
            {"response_excerpt": excerpt_txt.strip(),
             "note": "secret value redacted; pattern: " + name},
            "Remove the secret from the diff; rotate the credential "
            "immediately; never commit secrets."))
    files = re.findall(r"^diff --git a/(.+?) b/", text, re.M)
    if text.strip():
        added = sum(1 for ln in text.splitlines()
                    if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in text.splitlines()
                      if ln.startswith("-") and not ln.startswith("---"))
        findings.append(_new_finding(
            "p0_recon",
            f"Diff surface: {len(files)} file(s), "
            f"+{added}/-{removed} lines",
            "info", "confirmed",
            {"response_excerpt": ", ".join(files)[:2000],
             "note": "static inventory of the merge diff; no live "
                     "probing"},
            ""))
    else:
        findings.append(_new_finding(
            "p0_recon", "empty diff: nothing to scan",
            "info", "confirmed",
            {"response_excerpt": "",
             "note": "no diff text provided"}, ""))
    return findings


def _diff_mode_report(diff_text, note_title=None, note_detail=""):
    findings = scan_diff(diff_text or "")
    if note_title:
        findings.insert(0, _new_finding(
            "p0_recon", note_title, "info", "confirmed",
            {"response_excerpt": excerpt(str(note_detail), 1000),
             "note": "preview probing skipped; diff-only mode"}, ""))
    rep = {"decision": "pass",
           "findings": findings,
           "packs_run": ["p0_recon", "p3_leak"],
           "mode": "diff",
           "calls": 0}
    rep["decision"] = "block" if gate_blocks(rep) else "pass"
    return rep


async def run_merge_gate(http, target_url, diff_text="", max_calls=400):
    """Run the safe packs against target_url (a fork's preview
    deployment, speaking MCP over streamable HTTP).

    http: async callable (method, url, headers, timeout) -> {"status",
      "body", "headers"}. The JSON-RPC request is passed as a `body=`
      keyword argument (see module docstring).
    diff_text: the merge diff; when target_url is unreachable (or the
      seam cannot carry POST bodies), fall back to diff-only mode.
    max_calls: cap on total http calls; the run stops cleanly with an
      info finding when the budget is spent or on repeated 429/503.

    Returns {"decision": "pass"|"block", "findings": [...],
             "packs_run": [...], "mode": "preview"|"diff", "calls": n}.
    decision == "block" iff any finding has severity == "critical" and
    confidence == "confirmed".
    """
    if not _seam_accepts_body(http):
        return _diff_mode_report(
            diff_text,
            "preview probing unavailable: http seam has no request-body "
            "channel",
            "http(method, url, headers, timeout) accepts no body kwarg; "
            "MCP JSON-RPC probing requires POST bodies")
    run = _Run(http, target_url, max_calls)
    try:
        init = await run._rpc("initialize", {
            "protocolVersion": MCP_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
    except _StopProbing as e:
        run.add_info("p0_recon", "stopped during handshake", str(e))
        return run.report("preview")
    except Exception as e:
        return _diff_mode_report(
            diff_text,
            "target unreachable: falling back to diff-only mode",
            f"{type(e).__name__}: {e}")
    if not init.get("ok") or init.get("rpc_error"):
        return _diff_mode_report(
            diff_text,
            "handshake failed: falling back to diff-only mode",
            init.get("error") or _json(init.get("rpc_error")))
    await run._notify("notifications/initialized", {})
    try:
        tools = await run.pack_p0()
        await run.pack_p1(tools)
        await run.pack_p3(tools)
        await run.pack_p6(tools)
    except _StopProbing as e:
        run.add_info("merge_gate", "stopped early: " + str(e),
                     "partial results below; stopping is not an error")
    return run.report("preview")
