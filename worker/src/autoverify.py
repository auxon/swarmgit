"""Automatic verification of a submitted SwarmGit fork.

A solo outside agent should be able to claim, submit, and get a
verified pass without a human in the loop — but never by default-allow.
This module is the fail-closed auto-verifier:

  1. Clone the fork with a short-lived Artifacts *read* token and run
     the task's acceptance tests on the fork's own code inside an
     isolated Cloudflare Container (Durable Object VerifySandbox).
  2. Ask Clef to review the fork diff against the task description.
  3. Both must pass, with Clef confidence >= CONFIRM_THRESHOLD.
     Low confidence, missing bindings, timeouts, malformed answers,
     or sandbox/container failure → defer. Never invent a pass.

Isolation. `@cloudflare/sandbox` is TypeScript-only, so it is not
viable from this Python Worker. Cloudflare Containers are: the
SANDBOX Durable Object calls `ctx.container.exec` on the image in
`worker/Dockerfile`. Untrusted fork code never runs in the Worker
isolate. Clone and optional `pip install` are the only networked
steps; tests then run under `unshare --net` with CPU / wall-time /
memory limits. Any infra failure (missing binding, start/exec crash,
clone failure) defers — never allows. Preview HTTP is no longer a
pass path for isolated tests.

Bounties above AUTO_VERIFY_MAX_SATS (default 10_000) skip auto-verify
and wait for a staked human or agent verifier. Disputes stay on the
existing Clef / human path. The verifier recorded on a successful
(or failing) auto-attest is the reserved name `auto`, with evidence
(test output, Clef ruling, confidence) stored on the verification
row and shown on the board.
"""
import json
import time

import arbiter
import gitlib

AUTO_VERIFIER = "auto"
DEFAULT_MAX_SATS = 10_000
OUTPUT_CAP = 4000
DIFF_CAP = 8000
CLONE_TIMEOUT_S = 30
TEST_TIMEOUT_S = 60
TEST_CPU_SECONDS = 30
TEST_MEMORY_MIB = 512
READ_TOKEN_TTL_S = 300

# Paths we always try to diff when Artifacts cannot list the repo.
_COMMON_PATHS = (
    "README.md", "TASK.md", "server.py", "acceptance_tests.py",
    "main.py", "index.js", "index.ts", "package.json", "wrangler.toml",
    "src/index.py", "src/index.js", "src/entry.py", "worker.py",
)


class AutoVerifyError(Exception):
    pass


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def auto_verify_max_sats(deps):
    """Bounty ceiling (sats) at/below which auto-verify may run."""
    raw = deps.get("auto_verify_max_sats")
    if raw is None:
        return DEFAULT_MAX_SATS
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SATS
    return n if n >= 0 else DEFAULT_MAX_SATS


def should_auto_verify(task, deps):
    """True when this submission is eligible for the automatic path.

    Disabled explicitly (`auto_verify` false / AUTO_VERIFY=0), or when
    the bounty is above the configurable stake threshold. Missing
    sandbox/AI is not a skip: we still run and defer on failure.
    """
    if deps.get("auto_verify") is False:
        return False
    bounty = int((task or {}).get("bounty_sats") or 0)
    return bounty <= auto_verify_max_sats(deps)


def review_questions():
    """Clef schema for the auto-verify diff review (code, not a prompt)."""
    return {
        "implements": {
            "type": "noul",
            "instructions": "Does the submitted diff implement the task"
                            " description and its acceptance tests?"
                            " Cosmetic-only, empty, or unrelated"
                            " changes count as no.",
        },
        "ruling": {
            "type": "choice",
            "instructions": "Should automatic verification pass this"
                            " fork?",
            "criteria": {
                "pass": "The diff matches the task and stays in scope.",
                "fail": "The diff does not implement the task, or adds"
                        " unrelated or harmful changes.",
            },
        },
        "scope": {
            "type": "score",
            "instructions": "How well is the change scoped to the task?",
            "criteria": ["Unrelated", "Mostly extra", "On task", "Exact"],
        },
    }


def decide_review(answers):
    """Apply the 0.70 threshold. Returns ruling/confidence/deferred.

    Ruling must be pass|fail. Anything else, or confidence below
    arbiter.CONFIRM_THRESHOLD, defers. Never default-allow.
    """
    implements = answers.get("implements") or {}
    ruling_a = answers.get("ruling") or {}
    scope = answers.get("scope") or {}
    ruling = ruling_a.get("choice", "")
    conf = ruling_a.get("confidence", 0)
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    try:
        impl = float(implements.get("noul", 0) or 0)
    except (TypeError, ValueError):
        impl = 0.0
    out = {
        "ruling": ruling if ruling in ("pass", "fail") else "",
        "confidence": conf,
        "implements": impl,
        "scope": scope.get("score", 0),
        "deferred": True,
        "reason": "",
    }
    if ruling not in ("pass", "fail"):
        out["reason"] = "no clear review ruling from Clef"
        return out
    if conf < arbiter.CONFIRM_THRESHOLD:
        out["reason"] = (
            f"confidence {conf:.2f} below {arbiter.CONFIRM_THRESHOLD}")
        return out
    out["deferred"] = False
    return out


def interpret_acceptance_response(status, body):
    """Map an HTTP response to pass | fail | defer.

    Strict: only an explicit pass counts as pass. Transport errors,
    unknown shapes, and MCP error envelopes defer (infra / unknown
    protocol). `{ok: false}` or a FAIL line is a real test failure.
    """
    try:
        status = int(status or 0)
    except (TypeError, ValueError):
        status = 0
    text = "" if body is None else str(body)
    if status in (0, 502, 503, 504) or status >= 500:
        return "defer"
    if status in (401, 403, 404, 405):
        return "defer"
    stripped = text.strip()
    if stripped.startswith("PASS"):
        return "pass"
    if stripped.startswith("FAIL"):
        return "fail"
    try:
        data = json.loads(stripped)
    except Exception:
        return "defer"
    if not isinstance(data, dict):
        return "defer"
    if data.get("ok") is True or data.get("pass") is True \
            or data.get("verdict") == "pass":
        return "pass"
    if data.get("ok") is False or data.get("pass") is False \
            or data.get("verdict") == "fail":
        return "fail"
    return "defer"


def _cap(text, n=OUTPUT_CAP):
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _redact(text, secrets):
    text = "" if text is None else str(text)
    for s in secrets or []:
        s = "" if s is None else str(s)
        if s and s in text:
            text = text.replace(s, "«redacted»")
    return text


def _safe_remote(url):
    u = (url or "").strip()
    if u.startswith("https://") or u.startswith("http://"):
        return u
    return ""


def sandbox_limits(payload=None):
    p = payload or {}
    def _n(key, default):
        try:
            v = int(p.get(key) if p.get(key) is not None else default)
        except (TypeError, ValueError):
            v = default
        return v if v > 0 else default
    return {
        "timeout_s": _n("timeout_s", TEST_TIMEOUT_S),
        "cpu_seconds": _n("cpu_seconds", TEST_CPU_SECONDS),
        "memory_mib": _n("memory_mib", TEST_MEMORY_MIB),
        "clone_timeout_s": _n("clone_timeout_s", CLONE_TIMEOUT_S),
    }


def clone_script():
    """bash -c script. $1=remote $2=dest. Token on stdin. Never echoed."""
    return (
        'set -euo pipefail\n'
        'remote="$1"; dest="$2"\n'
        'IFS= read -r token\n'
        'export GIT_TERMINAL_PROMPT=0\n'
        'git -c http.extraHeader="Authorization: Bearer ${token}" '
        'clone --depth 1 -- "$remote" "$dest"\n'
        'unset token\n'
    )


def clone_argv(remote, dest="/tmp/fork", timeout_s=CLONE_TIMEOUT_S):
    return ["timeout", "--kill-after=5", str(int(timeout_s)),
            "bash", "-c", clone_script(), "clone", remote, dest]


def install_argv(timeout_s=60):
    return ["timeout", "--kill-after=5", str(int(timeout_s)),
            "bash", "-lc",
            "if [ -f /tmp/fork/requirements.txt ]; then "
            "pip install --no-cache-dir -q -r /tmp/fork/requirements.txt; "
            "fi"]


def test_argv(command, limits):
    """Run tests with no network (unshare --net) plus CPU/time/memory caps."""
    limits = sandbox_limits(limits)
    mem_kb = int(limits["memory_mib"]) * 1024
    inner = "ulimit -t %d; ulimit -v %d; cd /tmp/fork && %s" % (
        int(limits["cpu_seconds"]), mem_kb, command)
    return ["timeout", "--kill-after=5", str(int(limits["timeout_s"])),
            "unshare", "--net", "--", "bash", "-lc", inner]


def _preview_base(url):
    return (url or "").strip().rstrip("/")


def _test_specs(task):
    tests = (task or {}).get("acceptance_tests") or []
    out = []
    for t in tests:
        if isinstance(t, dict) and t.get("name"):
            out.append(t)
        elif isinstance(t, str) and t.strip():
            out.append({"name": t.strip()})
    return out


async def collect_diff(artifacts, base_repo, fork_repo):
    """Unified-ish text diff of files that changed between base and fork.

    Missing Artifacts, unlistable repos, or identical trees return an
    empty string — the Clef review then sees `diff unavailable` and
    (with the threshold rule) will not invent a pass.
    """
    if artifacts is None or not fork_repo:
        return ""
    paths = []
    lister = getattr(artifacts, "list_files", None)
    if callable(lister):
        try:
            listed = await lister(fork_repo, "main")
            if listed:
                paths.extend(str(p) for p in listed if p)
        except Exception:
            pass
    for p in _COMMON_PATHS:
        if p not in paths:
            paths.append(p)
    chunks = []
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        before = after = None
        try:
            if base_repo:
                before = await artifacts.read_file(base_repo, "main", path)
        except Exception:
            before = None
        try:
            after = await artifacts.read_file(fork_repo, "main", path)
        except Exception:
            after = None
        if before is None and after is None:
            continue
        if before == after:
            continue
        chunks.append(f"--- a/{path}\n+++ b/{path}")
        if before is None:
            chunks.append("+ (file added)")
            for line in str(after).splitlines()[:80]:
                chunks.append("+" + line)
        elif after is None:
            chunks.append("- (file removed)")
        else:
            for line in str(before).splitlines()[:40]:
                chunks.append("-" + line)
            for line in str(after).splitlines()[:40]:
                chunks.append("+" + line)
    return _cap("\n".join(chunks), DIFF_CAP)


async def _http(deps, method, url, headers=None, body=None, timeout=15):
    http = deps.get("http")
    if http is None:
        return {"ok": False, "status": 0, "body": "", "headers": {},
                "error": "http seam not configured"}
    headers = dict(headers or {})
    try:
        res = await http(method, url, headers, timeout, body=body)
    except TypeError:
        res = await http(method, url, headers, timeout)
    if not isinstance(res, dict):
        return {"ok": False, "status": 0, "body": "", "headers": {}}
    return res


async def run_preview_tests(deps, task, fork):
    """Drive named acceptance tests at the fork preview. Never evals code."""
    preview = _preview_base((fork or {}).get("preview_url") or "")
    specs = _test_specs(task)
    if not specs:
        return {"decision": "defer", "reason": "no acceptance tests",
                "output": "", "results": []}
    if not preview:
        return {"decision": "defer",
                "reason": "no preview_url and no sandbox",
                "output": "", "results": []}
    results = []
    lines = []
    for spec in specs:
        name = spec.get("name") or "unnamed"
        path = (spec.get("path") or "").strip()
        if path:
            url = preview + (path if path.startswith("/") else "/" + path)
            method = (spec.get("method") or "GET").upper()
            body = spec.get("body")
            if body is not None and not isinstance(body, str):
                body = json.dumps(body)
        else:
            url = preview + "/__acceptance"
            method = "POST"
            body = json.dumps({"name": name})
        headers = {"Content-Type": "application/json"}
        res = await _http(deps, method, url, headers, body=body)
        status = res.get("status", 0)
        text = res.get("body") or res.get("error") or ""
        hdrs = res.get("headers") or {}
        verdict = interpret_acceptance_response(status, text)
        expect_status = spec.get("expect_status")
        if verdict == "pass" and expect_status is not None:
            try:
                if int(status) != int(expect_status):
                    verdict = "fail"
            except (TypeError, ValueError):
                verdict = "defer"
        expect_header = spec.get("expect_header")
        if verdict == "pass" and expect_header:
            lower = {str(k).lower(): str(v) for k, v in hdrs.items()}
            if str(expect_header).lower() not in lower:
                verdict = "fail"
        results.append({"name": name, "verdict": verdict,
                        "status": status, "body": _cap(text, 500)})
        lines.append(f"{verdict.upper()} {name} — HTTP {status}")
    verdicts = [r["verdict"] for r in results]
    output = "\n".join(lines)
    if any(v == "fail" for v in verdicts):
        return {"decision": "fail", "reason": "acceptance test failed",
                "output": output, "results": results}
    if any(v != "pass" for v in verdicts):
        return {"decision": "defer",
                "reason": "acceptance tests incomplete or unknown protocol",
                "output": output, "results": results}
    return {"decision": "pass", "reason": "",
            "output": output, "results": results}


async def issue_read_token(artifacts, repo_name, ttl_s=READ_TOKEN_TTL_S):
    """Short-lived read token for the sandbox clone. Never persisted."""
    if artifacts is None or not repo_name:
        return ""
    issuer = getattr(artifacts, "issue_token", None)
    if not callable(issuer):
        return ""
    try:
        tok = await issuer(repo_name, ttl_s=int(ttl_s), scope="read")
    except TypeError:
        try:
            tok = await issuer(repo_name, int(ttl_s), "read")
        except Exception:
            return ""
    except Exception:
        return ""
    return (tok or "").strip()


async def run_sandbox_tests(sandbox, task, fork, files=None, artifacts=None):
    """Ask the container sandbox to clone the fork and exec tests."""
    specs = _test_specs(task)
    names = [s["name"] for s in specs]
    command = (task.get("test_command") if isinstance(task, dict) else "") \
        or "python3 acceptance_tests.py"
    remote = _safe_remote((fork or {}).get("remote") or "")
    repo_name = (fork or {}).get("repo_name", "")
    token = await issue_read_token(artifacts, repo_name)
    if not remote:
        return {"decision": "defer",
                "reason": "fork has no clone remote",
                "output": "", "results": []}
    if not token:
        return {"decision": "defer",
                "reason": "could not issue a short-lived read token",
                "output": "", "results": []}
    payload = {
        "fork_id": (fork or {}).get("fork_id", ""),
        "repo_name": repo_name,
        "remote": remote,
        "clone_token": token,
        "command": command,
        "tests": names,
        "files": files or {},
        "timeout_s": TEST_TIMEOUT_S,
        "cpu_seconds": TEST_CPU_SECONDS,
        "memory_mib": TEST_MEMORY_MIB,
        "isolate_network": True,
    }
    try:
        raw = await sandbox.run_tests(payload)
    except Exception as e:
        return {"decision": "defer",
                "reason": f"sandbox failed: {type(e).__name__}",
                "output": "", "results": []}
    if not isinstance(raw, dict):
        return {"decision": "defer", "reason": "sandbox returned no result",
                "output": "", "results": []}
    if raw.get("deferred") or raw.get("decision") == "defer":
        return {"decision": "defer",
                "reason": _redact(raw.get("reason") or "sandbox deferred",
                                  [token]),
                "output": _cap(_redact(raw.get("output") or "", [token])),
                "results": raw.get("results") or []}
    output = _cap(_redact(raw.get("output") or "", [token]))
    code = raw.get("exit_code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if raw.get("decision") in ("pass", "fail"):
        return {"decision": raw["decision"],
                "reason": raw.get("reason") or "",
                "output": output, "results": raw.get("results") or []}
    if code == 0:
        return {"decision": "pass", "reason": "", "output": output,
                "results": raw.get("results") or []}
    if code is None:
        return {"decision": "defer",
                "reason": raw.get("reason") or "sandbox produced no exit code",
                "output": output, "results": raw.get("results") or []}
    return {"decision": "fail", "reason": f"tests exited {code}",
            "output": output, "results": raw.get("results") or []}


async def _clef_review(deps, task, diff_text, test_result):
    try:
        answers = await arbiter.ask_clef(
            deps.get("ai"),
            {
                "title": (task or {}).get("title", ""),
                "description": (task or {}).get("description", ""),
                "acceptance_tests": [
                    s.get("name") for s in _test_specs(task)],
                "diff": diff_text or "(diff unavailable)",
                "tests": {
                    "decision": test_result.get("decision"),
                    "output": _cap(test_result.get("output"), 1500),
                },
            },
            review_questions())
        return decide_review(answers)
    except arbiter.ArbiterError as e:
        return {"ruling": "", "confidence": 0.0, "implements": 0.0,
                "scope": 0, "deferred": True,
                "reason": f"Clef unavailable: {e}"}


def _evidence(test_result, review, diff_text, decision, reason):
    return {
        "tests": {
            "decision": test_result.get("decision"),
            "output": _cap(test_result.get("output")),
            "results": test_result.get("results") or [],
            "reason": test_result.get("reason", ""),
        },
        "clef": {
            "ruling": review.get("ruling", ""),
            "confidence": review.get("confidence", 0.0),
            "implements": review.get("implements", 0.0),
            "deferred": bool(review.get("deferred")),
            "reason": review.get("reason", ""),
        },
        "diff_excerpt": _cap(diff_text, 1500),
        "decision": decision,
        "reason": reason,
        "at": int(time.time()),
    }


async def run_auto_verify(store, deps, task, fork):
    """Run tests + Clef review. Returns a decision record.

    decision: pass | fail | defer
    Isolated tests require the SANDBOX container. Missing or failing
    sandbox defers — preview HTTP is never a pass path.
    """
    sandbox = deps.get("sandbox")
    artifacts = deps.get("artifacts")
    files = {}
    if artifacts is not None and fork.get("repo_name"):
        lister = getattr(artifacts, "list_files", None)
        if callable(lister):
            try:
                for path in (await lister(fork["repo_name"], "main")) or []:
                    try:
                        text = await artifacts.read_file(
                            fork["repo_name"], "main", path)
                    except Exception:
                        text = None
                    if text is not None:
                        files[str(path)] = text
            except Exception:
                files = {}
        if not files:
            for path in _COMMON_PATHS:
                try:
                    text = await artifacts.read_file(
                        fork["repo_name"], "main", path)
                except Exception:
                    text = None
                if text is not None:
                    files[path] = text

    if sandbox is None:
        test_result = {"decision": "defer",
                       "reason": "sandbox not bound — isolated tests required",
                       "output": "", "results": []}
    else:
        test_result = await run_sandbox_tests(
            sandbox, task, fork, files, artifacts=artifacts)

    diff_text = await collect_diff(
        artifacts, (task or {}).get("repo", ""),
        (fork or {}).get("repo_name", ""))

    # Objective test failure is a fail even before Clef — reproduce-to-
    # release still applies (the test output is the repro). Infra
    # deferral skips Clef so a missing preview does not burn the model.
    if test_result.get("decision") == "defer":
        evidence = _evidence(test_result, {
            "ruling": "", "confidence": 0.0, "deferred": True,
            "reason": "tests did not complete"}, diff_text, "defer",
            test_result.get("reason") or "tests deferred")
        return {"decision": "defer",
                "reason": evidence["reason"],
                "repro": "",
                "evidence": evidence}

    review = await _clef_review(deps, task, diff_text, test_result)
    if test_result.get("decision") == "fail":
        reason = test_result.get("reason") or "acceptance tests failed"
        evidence = _evidence(test_result, review, diff_text, "fail", reason)
        return {"decision": "fail", "reason": reason,
                "repro": _cap(test_result.get("output") or reason, 1500),
                "evidence": evidence}
    if review.get("deferred"):
        reason = review.get("reason") or "Clef review deferred"
        evidence = _evidence(test_result, review, diff_text, "defer", reason)
        return {"decision": "defer", "reason": reason, "repro": "",
                "evidence": evidence}
    if review.get("ruling") == "fail":
        reason = "Clef review failed the diff"
        evidence = _evidence(test_result, review, diff_text, "fail", reason)
        return {"decision": "fail", "reason": reason,
                "repro": reason, "evidence": evidence}
    if review.get("ruling") != "pass" or test_result.get("decision") != "pass":
        reason = "auto-verify incomplete — deferred"
        evidence = _evidence(test_result, review, diff_text, "defer", reason)
        return {"decision": "defer", "reason": reason, "repro": "",
                "evidence": evidence}
    evidence = _evidence(test_result, review, diff_text, "pass", "")
    return {"decision": "pass", "reason": "", "repro": "",
            "evidence": evidence}


async def apply_auto_verify(store, deps, task, fork):
    """Run auto-verify and attest as `auto` on pass/fail. Defer otherwise.

    Returns the decision record. On pass, the attestation's
    merge_payload is attached as `merge_payload` when ready.
    """
    result = await run_auto_verify(store, deps, task, fork)
    evidence = result.get("evidence") or {}
    fork_id = (fork or {}).get("fork_id", "")
    if result["decision"] == "pass":
        att = await gitlib.attest_verification(
            store, fork_id, AUTO_VERIFIER, "pass", 0, "",
            evidence=evidence)
        result["attestation"] = att
        if att.get("ready_for_merge"):
            result["merge_payload"] = att.get("merge_payload")
        return result
    if result["decision"] == "fail":
        att = await gitlib.attest_verification(
            store, fork_id, AUTO_VERIFIER, "fail", 0,
            result.get("repro") or result.get("reason") or "auto-verify fail",
            evidence=evidence)
        result["attestation"] = att
        return result
    await store.ledger_add(
        "auto_verify_deferred", (task or {}).get("task_id", ""),
        AUTO_VERIFIER, 0,
        {"fork_id": fork_id, "reason": result.get("reason", ""),
         "clef": (evidence.get("clef") or {}),
         "tests": (evidence.get("tests") or {}).get("decision", ""),
         "settlement": "dry_run"})
    return result


class FakeSandbox:
    """Test double. Canned decision, records every run_tests payload."""

    def __init__(self, decision="pass", output="PASS all", exit_code=0,
                 reason="", deferred=False):
        self.decision = decision
        self.output = output
        self.exit_code = exit_code
        self.reason = reason
        self.deferred = deferred
        self.calls = []

    async def run_tests(self, payload):
        self.calls.append(payload)
        if self.deferred:
            return {"deferred": True, "reason": self.reason or "sandbox down",
                    "output": self.output}
        return {"decision": self.decision, "output": self.output,
                "exit_code": self.exit_code, "reason": self.reason}


try:
    from workers import DurableObject as _DurableObject
except Exception:  # CPython tests: no workers runtime
    class _DurableObject:  # noqa: N801
        def __init__(self, ctx=None, env=None):
            self.ctx = ctx
            self.env = env


async def _await_maybe(value):
    if value is not None and hasattr(value, "__await__"):
        return await value
    return value


def _decode_bytes(v):
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return "" if v is None else str(v)


def _exec_result(out):
    stdout = _decode_bytes(getattr(out, "stdout", b"") or b"")
    stderr = _decode_bytes(getattr(out, "stderr", b"") or b"")
    code = getattr(out, "exitCode", None)
    if code is None:
        code = getattr(out, "exit_code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return code, stdout, stderr


async def _container_exec(container, argv, opts=None):
    if opts:
        proc = await container.exec(argv, opts)
    else:
        proc = await container.exec(argv)
    out = await proc.output() if hasattr(proc, "output") else proc
    return _exec_result(out)


async def _container_destroy(container):
    destroy = getattr(container, "destroy", None)
    if not callable(destroy):
        return
    try:
        await _await_maybe(destroy())
    except Exception:
        pass


class VerifySandbox(_DurableObject):
    """Durable Object: clone a fork and exec tests in a Container.

    Bound by wrangler.toml `SANDBOX` + [[containers]] image. Without a
    container the RPC returns deferred — never a pass. Clone uses a
    short-lived read token on stdin (never logged). Tests run under
    `unshare --net` with CPU / time / memory limits.
    """

    def __init__(self, ctx=None, env=None):
        try:
            super().__init__(ctx, env)
        except TypeError:
            pass
        self.ctx = ctx
        self.env = env

    async def run_tests(self, payload):
        payload = payload or {}
        token = (payload.get("clone_token") or "").strip()
        remote = _safe_remote(payload.get("remote") or "")
        command = payload.get("command") or "python3 acceptance_tests.py"
        limits = sandbox_limits(payload)
        container = getattr(self.ctx, "container", None) if self.ctx else None
        if container is None:
            return {"deferred": True,
                    "reason": "container not configured on SANDBOX DO"}
        if not remote:
            return {"deferred": True, "reason": "fork has no clone remote"}
        if not token:
            return {"deferred": True,
                    "reason": "missing short-lived read token"}
        try:
            if getattr(container, "running", False):
                await _container_destroy(container)
            start = getattr(container, "start", None)
            if start is None:
                return {"deferred": True, "reason": "container cannot start"}
            opts = {
                "enableInternet": True,
                "entrypoint": ["sleep", "infinity"],
                "instance": {"vcpu": 0.25, "memoryMib": 1024, "diskMb": 4000},
            }
            images = getattr(container, "images", None)
            if images is not None:
                img = getattr(images, "sandbox", None) \
                    or getattr(images, "base", None) \
                    or getattr(images, "workspace", None)
                if img:
                    opts["image"] = img
            await _await_maybe(start(opts))
            idle = getattr(container, "setInactivityTimeout", None)
            if callable(idle):
                try:
                    await _await_maybe(idle(3 * 60 * 1000))
                except Exception:
                    pass

            code, stdout, stderr = await _container_exec(
                container,
                clone_argv(remote, "/tmp/fork", limits["clone_timeout_s"]),
                {"stdin": token + "\n"})
            combined = _redact(
                _cap(str(stdout) + ("\n" + str(stderr) if stderr else "")),
                [token])
            if code != 0:
                return {"deferred": True, "exit_code": code,
                        "reason": "clone failed (infra — deferred)",
                        "output": combined}

            if payload.get("install") is not False:
                icode, iout, ierr = await _container_exec(
                    container, install_argv(limits["clone_timeout_s"]))
                if icode not in (0, None):
                    combined = _redact(
                        _cap(combined + "\n" + str(iout)
                             + ("\n" + str(ierr) if ierr else "")),
                        [token])
                    return {"deferred": True, "exit_code": icode,
                            "reason": "package install failed (infra — deferred)",
                            "output": combined}

            tcode, tout, terr = await _container_exec(
                container, test_argv(command, limits))
            combined = _redact(
                _cap(str(tout) + ("\n" + str(terr) if terr else "")),
                [token])
            if tcode is None:
                return {"deferred": True,
                        "reason": "sandbox produced no exit code",
                        "output": combined}
            # unshare not permitted / timeout wrapper missing → infra defer
            if tcode in (124, 125, 126, 127) and "unshare" in (terr or ""):
                return {"deferred": True, "exit_code": tcode,
                        "reason": "network isolation failed (infra — deferred)",
                        "output": combined}
            decision = "pass" if tcode == 0 else "fail"
            reason = "" if tcode == 0 else (
                "tests timed out" if tcode == 124
                else "tests exited %s" % tcode)
            return {"decision": decision, "exit_code": tcode,
                    "reason": reason, "output": combined}
        except Exception as e:
            return {"deferred": True,
                    "reason": f"container exec failed: {type(e).__name__}"}
        finally:
            try:
                await _container_destroy(container)
            except Exception:
                pass


def sandbox_from_env(env):
    """Build a sandbox RPC adapter from env.SANDBOX, or None."""
    try:
        ns = getattr(env, "SANDBOX", None)
    except Exception:
        ns = None
    if ns is None:
        return None

    class _Adapter:
        def __init__(self, namespace):
            self.ns = namespace

        async def run_tests(self, payload):
            name = str((payload or {}).get("fork_id") or "default")[:64]
            obj_id = self.ns.idFromName(name)
            stub = self.ns.get(obj_id)
            return await stub.run_tests(payload)

    return _Adapter(ns)
