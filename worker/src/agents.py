"""Agent credentials. A token is shown once and stored as a hash.

Operator bearer and Google sessions post tasks and close disputes.
An agent token may claim, submit, attest, and dispute as itself.
Reads and register_agent need no credential.
"""
import hashlib
import secrets

PREFIX = "sga_"
PUBLIC_TOOLS = {"register_agent", "list_tasks", "get_task_status",
                "get_leaderboard"}
AGENT_TOOLS = PUBLIC_TOOLS | {
    "claim_task", "submit_work", "attest_verification",
    "dispute_fork", "get_merge_report",
}
SELF_TOOLS = {"claim_task", "submit_work", "attest_verification",
              "dispute_fork"}


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token():
    return PREFIX + secrets.token_hex(24)


def bearer(request):
    try:
        auth = request.headers.get("Authorization") or ""
    except Exception:
        return ""
    if str(auth).startswith("Bearer "):
        return str(auth)[len("Bearer "):].strip()
    return ""


def public_call(body):
    if not isinstance(body, dict):
        return False
    method = body.get("method")
    if method in ("initialize", "notifications/initialized", "ping", "tools/list"):
        return True
    if method != "tools/call":
        return False
    name = ((body.get("params") or {}).get("name") or "")
    return name in PUBLIC_TOOLS
