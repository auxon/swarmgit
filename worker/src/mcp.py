"""MCP Streamable HTTP (stateless) JSON-RPC dispatch for SwarmGit.

Same plumbing as the TestSwarm/PreFlight scaffolds (no `mcp` package —
hand-rolled tools-only server):

  POST /mcp  JSON-RPC 2.0: initialize, notifications/initialized, ping,
             tools/list, tools/call
  GET /mcp   -> 405 (stateless: no SSE streams)

dispatch(store, deps, body) -> (http_status, response_dict | None).
deps: {"enqueue": async fn, "http": async fn, "artifacts": backend,
       "sender": Sender}. Tests inject fakes.
"""
import json
import time

import gitlib
from gitlib import GitError, nid
import whygraph

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "SwarmGit"
SERVER_VERSION = "0.1.0"

DRY_RUN = ("Settlement is DRY-RUN ONLY: escrow and payouts are ledger"
           " entries; no sats move until Richard's POST /agent/send"
           " primitive exists. This tool cannot move funds.")


def _req_int(args, name, minimum=0):
    v = args.get(name)
    if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
        raise GitError(f"refused: {name} must be an int >= {minimum}")
    return v


TOOLS = [
    {
        "name": "post_task",
        "description": (
            "Post a coding task with a sats bounty: Artifacts repo, title,"
            " description, acceptance_tests (each needs a name),"
            " bounty_sats, optional deadline_at (unix).\n"
            "Bounty is escrowed on posting (ledger-only). " + DRY_RUN +
            "\nIdempotent: repeating with the same idempotency_key returns"
            " the original task, never a duplicate."),
        "inputSchema": {
            "type": "object",
            "required": ["repo", "title", "acceptance_tests",
                         "bounty_sats", "idempotency_key"],
            "properties": {
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string", "default": ""},
                "acceptance_tests": {"type": "array"},
                "bounty_sats": {"type": "integer", "minimum": 0},
                "deadline_at": {"type": "integer", "minimum": 0,
                                "default": 0},
                "poster": {"type": "string", "default": "anon"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "list_tasks",
        "description": (
            "List coding tasks (id, title, repo, bounty, status, claim"
            " count). Filter by status: open, claimed, working, submitted,"
            " verifying, merging, settled, expired, disputed.\n"
            "Read-only. Cannot move funds or modify anything."),
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string", "default": ""}},
        },
    },
    {
        "name": "claim_task",
        "description": (
            "Claim an open task as an agent: mints a private Artifacts fork"
            " with a short-lived repo-scoped token for your work. Exactly"
            " one claim per agent per task — a second claim is refused"
            " (no double-work by construction).\n"
            "The repo token is returned ONLY in this response — store it;"
            " later reads show last4 only.\n"
            "Idempotent: repeating with the same idempotency_key returns"
            " the original claim, never a second fork."),
        "inputSchema": {
            "type": "object",
            "required": ["task_id", "agent", "idempotency_key"],
            "properties": {
                "task_id": {"type": "string"},
                "agent": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "get_task_status",
        "description": (
            "Poll a task: state-machine status, claims, forks (with"
            " preview URLs and statuses), verifications, merges, and the"
            " dry-run ledger slice.\nRead-only. Cannot move funds."),
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string"}},
        },
    },
    {
        "name": "submit_work",
        "description": (
            "Submit your fork's work for verification: registers the fork"
            " push (or a preview_url) and queues verification. The claim"
            " must be yours and active.\n"
            "Idempotent: repeating with the same idempotency_key returns"
            " the original submission."),
        "inputSchema": {
            "type": "object",
            "required": ["task_id", "claim_id", "agent",
                         "idempotency_key"],
            "properties": {
                "task_id": {"type": "string"},
                "claim_id": {"type": "string"},
                "agent": {"type": "string"},
                "preview_url": {"type": "string", "default": ""},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "attest_verification",
        "description": (
            "Attest as a verifier that a fork passes or fails the"
            " acceptance tests. Your stake_sats are locked (ledger-only)."
            " A 'fail' verdict MUST include a repro"
            " (reproduce-to-release). You cannot verify your own work;\n"
            " one attestation per verifier per fork. First verified pass"
            " wins the task and queues the merge gate.\n" + DRY_RUN +
            "\nIdempotent: repeating with the same idempotency_key returns"
            " the original attestation."),
        "inputSchema": {
            "type": "object",
            "required": ["fork_id", "verifier", "verdict",
                         "idempotency_key"],
            "properties": {
                "fork_id": {"type": "string"},
                "verifier": {"type": "string"},
                "verdict": {"type": "string",
                            "enum": ["pass", "fail"]},
                "stake_sats": {"type": "integer", "minimum": 0,
                               "default": 0},
                "repro": {"type": "string", "default": ""},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "get_merge_report",
        "description": (
            "Read a task's merge record: decision (merged | gate_blocked),"
            " the merge-gate audit report (findings, severity, evidence),"
            " verifier attestations, and the staged why-graph entries"
            " (rationale log behind the decision).\n"
            "Read-only. Cannot move funds."),
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string"}},
        },
    },
    {
        "name": "get_leaderboard",
        "description": (
            "Agent reputation leaderboard: score, tasks won, tasks"
            " verified, false reports.\nRead-only. Cannot move funds."),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1,
                                    "maximum": 100, "default": 25}},
        },
    },
]
_TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------- JSON-RPC plumbing

def _ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def _tool_result(payload):
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, default=str)}],
            "isError": False}


def _tool_error(message):
    return {"content": [{"type": "text", "text": json.dumps(
        {"ok": False, "error": message})}],
        "isError": True}


async def dispatch(store, deps, body):
    """Handle one JSON-RPC message. Returns (http_status, response|None)."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600,
                               "message": "invalid JSON-RPC 2.0 message"}}
    msg_id = body.get("id")
    method = body.get("method")

    if method == "notifications/initialized":
        return 202, None
    if method == "initialize":
        params = body.get("params") or {}
        offered = params.get("protocolVersion")
        version = (offered if offered in ("2025-06-18", "2025-03-26",
                                          "2024-11-05")
                   else PROTOCOL_VERSION)
        return 200, _ok(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME,
                           "version": SERVER_VERSION}})
    if method == "ping":
        return 200, _ok(msg_id, {})
    if method == "tools/list":
        return 200, _ok(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = body.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in _TOOLS_BY_NAME:
            return 200, _err(msg_id, -32601, f"unknown tool: {name}")
        try:
            payload = await _call_tool(store, deps, name, args)
        except GitError as e:
            return 200, _ok(msg_id, _tool_error(str(e)))
        except Exception as e:  # never leak a traceback to the client
            return 200, _ok(msg_id, _tool_error(
                f"internal error: {type(e).__name__}"))
        return 200, _ok(msg_id, _tool_result(payload))
    return 200, _err(msg_id, -32601, f"method not found: {method}")


# ---------------------------------------------------------------- tools

def _idem_hit(store_rec, tool):
    if store_rec and store_rec.get("tool") == tool:
        result = dict(store_rec["result"])
        result["idempotent"] = True
        return result
    return None


def _need_key(args):
    key = args.get("idempotency_key") or ""
    if not key:
        raise GitError("refused: idempotency_key is required")
    return key


async def _call_tool(store, deps, name, args):
    if name == "post_task":
        return await _post_task(store, args)
    if name == "list_tasks":
        return await _list_tasks(store, args)
    if name == "claim_task":
        return await _claim_task(store, deps, args)
    if name == "get_task_status":
        return await _get_task_status(store, args)
    if name == "submit_work":
        return await _submit_work(store, deps, args)
    if name == "attest_verification":
        return await _attest_verification(store, deps, args)
    if name == "get_merge_report":
        return await _get_merge_report(store, args)
    if name == "get_leaderboard":
        return await _get_leaderboard(store, args)
    raise GitError(f"unknown tool: {name}")  # unreachable


async def _post_task(store, args):
    key = _need_key(args)
    hit = _idem_hit(await store.idem_get(key), "post_task")
    if hit:
        return hit
    if not await store.rate_allow("post_task", limit=20, window=3600):
        raise GitError("refused: rate limit — 20 tasks/hour")
    spec = {
        "repo": args.get("repo", ""),
        "title": args.get("title", ""),
        "description": args.get("description", ""),
        "acceptance_tests": args.get("acceptance_tests"),
        "bounty_sats": args.get("bounty_sats"),
        "deadline_at": args.get("deadline_at", 0),
        "poster": args.get("poster", "anon"),
    }
    task = await gitlib.post_task(store, spec)
    result = {"ok": True, "task_id": task["task_id"], "repo": task["repo"],
              "bounty_sats": task["bounty_sats"], "status": task["status"],
              "escrow": "dry_run — ledger only, no funds moved",
              "idempotent": False}
    await store.idem_put(key, "post_task", result)
    return result


async def _list_tasks(store, args):
    status = (args.get("status") or "").strip() or None
    tasks = await store.list_tasks(status)
    out = []
    for t in tasks:
        claims = await store.claims_for_task(t["task_id"])
        out.append({"task_id": t["task_id"], "title": t.get("title", ""),
                    "repo": t.get("repo", ""), "status": t.get("status"),
                    "bounty_sats": t.get("bounty_sats", 0),
                    "claims": len(claims),
                    "created_at": t.get("created_at")})
    return {"ok": True, "tasks": out}


async def _claim_task(store, deps, args):
    key = _need_key(args)
    hit = _idem_hit(await store.idem_get(key), "claim_task")
    if hit:
        return hit
    task_id = args.get("task_id", "")
    agent = args.get("agent", "")
    if not task_id or not agent:
        raise GitError("refused: task_id and agent are required")
    res = await gitlib.claim_task(store, deps["artifacts"], task_id, agent)
    token = res.pop("_token")
    result = {"ok": True, "claim_id": res["claim_id"],
              "fork_id": res["fork_id"], "repo_name": res["repo_name"],
              "repo_token": token,
              "repo_token_note": "short-lived, repo-scoped; shown once —"
                                 " later reads show last4 only",
              "idempotent": False}
    await store.idem_put(key, "claim_task", {k: v for k, v in
                                             result.items()
                                             if k != "repo_token"})
    return result


async def _get_task_status(store, args):
    task_id = args.get("task_id", "")
    task = await store.get_task(task_id)
    if not task:
        raise GitError("task not found")
    claims = await store.claims_for_task(task_id)
    forks = await store.forks_for_task(task_id)
    for f in forks:
        f["verifications"] = await store.verifications_for_fork(
            f["fork_id"])
    merges = await store.merges_for_task(task_id)
    return {"ok": True,
            "task": {"task_id": task["task_id"],
                     "title": task.get("title", ""),
                     "repo": task.get("repo", ""),
                     "status": task.get("status"),
                     "bounty_sats": task.get("bounty_sats", 0),
                     "poster": task.get("poster", "anon")},
            "claims": [{"id": c["id"], "agent": c["agent"],
                        "status": c["status"]} for c in claims],
            "forks": [{"fork_id": f["fork_id"],
                       "repo_name": f.get("repo_name", ""),
                       "preview_url": f.get("preview_url", ""),
                       "status": f.get("status"),
                       "verifications": f["verifications"]} for f in forks],
            "merges": [{"merge_id": m["merge_id"],
                        "decision": m.get("decision"),
                        "fork_id": m.get("fork_id")} for m in merges],
            "ledger": await store.ledger_for_task(task_id)}


async def _submit_work(store, deps, args):
    key = _need_key(args)
    hit = _idem_hit(await store.idem_get(key), "submit_work")
    if hit:
        return hit
    payload = await gitlib.submit_work(
        store, args.get("task_id", ""), args.get("claim_id", ""),
        args.get("agent", ""), args.get("preview_url", ""))
    await deps["enqueue"](payload)
    result = {"ok": True, "task_id": args.get("task_id", ""),
              "fork_id": payload["fork_id"], "status": "submitted",
              "verify_queued": True, "idempotent": False}
    await store.idem_put(key, "submit_work", result)
    return result


async def _attest_verification(store, deps, args):
    key = _need_key(args)
    hit = _idem_hit(await store.idem_get(key), "attest_verification")
    if hit:
        return hit
    res = await gitlib.attest_verification(
        store, args.get("fork_id", ""), args.get("verifier", ""),
        args.get("verdict", ""), args.get("stake_sats", 0),
        args.get("repro", ""))
    if res.get("ready_for_merge"):
        await deps["enqueue"](res["merge_payload"])
    result = {"ok": True, "fork_id": args.get("fork_id", ""),
              "verdict": res.get("verdict"),
              "merge_queued": bool(res.get("ready_for_merge")),
              "note": res.get("note", ""),
              "idempotent": False}
    await store.idem_put(key, "attest_verification", result)
    return result


async def _get_merge_report(store, args):
    task_id = args.get("task_id", "")
    merges = await store.merges_for_task(task_id)
    if not merges:
        raise GitError("no merges recorded for this task")
    out = []
    for m in merges:
        why = await store.why_entries_for_merge(m["merge_id"])
        out.append({"merge_id": m["merge_id"], "decision": m["decision"],
                    "fork_id": m["fork_id"],
                    "audit_report": m.get("audit_report", {}),
                    "why_refs": m.get("why_refs", []),
                    "why_entries": why,
                    "merged_at": m.get("merged_at")})
    return {"ok": True, "task_id": task_id, "merges": out}


async def _get_leaderboard(store, args):
    limit = args.get("limit", 25)
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = 25
    limit = max(1, min(100, limit))
    return {"ok": True, "leaderboard": await store.leaderboard(limit)}
