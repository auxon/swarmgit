"""Spending controls and kill switch for SwarmGit.

Live settlement is DryRun unless ALL of these hold:

  * SWARMSGIT_LIVE_SETTLEMENT is truthy
  * AGENTPAY_KEY or AGENTPAY_AGENT_KEY is set
  * every cap env var is configured (empty is not configured; 0 is)
  * the operator pause is off (env SWARMSGIT_PAUSED or D1 meta)

Caps (sats, integers):

  SWARMSGIT_PAYOUT_CAP_SATS          per payout
  SWARMSGIT_DAILY_CAP_SATS           all live/internal releases, 24h
  SWARMSGIT_WEEKLY_CAP_SATS          all live/internal releases, 7d
  SWARMSGIT_AGENT_DAILY_CAP_SATS     per-agent live/internal, 24h
  SWARMSGIT_APPROVAL_THRESHOLD_SATS  payouts above this wait on /board

Pause stops new claims, auto-verify merge enqueue, and releases.
Toggle via MCP `set_pause` or POST /admin/pause (operator bearer).
"""
import time

CAP_VARS = (
    "SWARMSGIT_PAYOUT_CAP_SATS",
    "SWARMSGIT_DAILY_CAP_SATS",
    "SWARMSGIT_WEEKLY_CAP_SATS",
    "SWARMSGIT_AGENT_DAILY_CAP_SATS",
    "SWARMSGIT_APPROVAL_THRESHOLD_SATS",
)

DAY = 86400
WEEK = 7 * DAY

# Live send blocked for these reasons must not enqueue a walletd puller.
NO_PULLER_CODES = (
    "paused", "caps_unconfigured", "payout_cap", "daily_cap",
    "weekly_cap", "agent_daily_cap", "needs_approval", "approval_rejected",
)


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def env_get(env, name, default=""):
    try:
        return getattr(env, name, default)
    except Exception:
        return default


def env_int(env, name):
    """Configured integer, or None if the var is missing/blank."""
    raw = env_get(env, name, None)
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def agentpay_key(env):
    key = (env_get(env, "AGENTPAY_KEY", "")
           or env_get(env, "AGENTPAY_AGENT_KEY", "")
           or "")
    return str(key).strip()


def caps_configured(env):
    return all(env_int(env, name) is not None for name in CAP_VARS)


def live_armed(env):
    """True only when flag + key + every cap is set. Pause is separate."""
    return _truthy(env_get(env, "SWARMSGIT_LIVE_SETTLEMENT", "")) \
        and bool(agentpay_key(env)) \
        and caps_configured(env)


def missing_live_requirements(env):
    missing = []
    if not _truthy(env_get(env, "SWARMSGIT_LIVE_SETTLEMENT", "")):
        missing.append("SWARMSGIT_LIVE_SETTLEMENT")
    if not agentpay_key(env):
        missing.append("AGENTPAY_KEY")
    for name in CAP_VARS:
        if env_int(env, name) is None:
            missing.append(name)
    return missing


async def is_paused(env, store):
    if _truthy(env_get(env, "SWARMSGIT_PAUSED", "")):
        return True
    getter = getattr(store, "get_meta", None) if store is not None else None
    if not callable(getter):
        return False
    try:
        return _truthy(await getter("paused"))
    except Exception:
        return False


async def set_paused(store, paused, operator=""):
    setter = getattr(store, "set_meta", None)
    if not callable(setter):
        raise RuntimeError("store cannot persist pause")
    await setter("paused", "1" if paused else "0")
    await store.ledger_add(
        "spend_pause" if paused else "spend_resume",
        "", operator or "operator", 0,
        {"paused": bool(paused), "operator": operator or "operator",
         "settlement": "dry_run"})
    return {"ok": True, "paused": bool(paused), "operator": operator}


def is_released_settlement(settlement):
    return (settlement or "") in ("live", "internal")


async def released_sats(store, since_t, agent=None):
    """Sum live/internal payouts (and walletd release_done) since since_t."""
    rows = await store.ledger_all()
    total = 0
    for r in rows:
        if int(r.get("t") or 0) < int(since_t):
            continue
        if agent is not None and r.get("agent") != agent:
            continue
        kind = r.get("kind")
        settlement = r.get("settlement") or ""
        if kind == "payout" and is_released_settlement(settlement):
            total += int(r.get("sats") or 0)
        elif kind == "release_done" and is_released_settlement(settlement):
            total += int(r.get("sats") or 0)
    return total


async def gate_payout(env, store, agent, sats, task_id=""):
    """None if the payout may proceed. Else a {code, reason} dict.

    Dry-run callers should not invoke this. Live callers must.
    """
    sats = int(sats or 0)
    agent = (agent or "").strip()
    if await is_paused(env, store):
        return {"code": "paused",
                "reason": "SwarmGit is paused — releases are frozen"}
    if not caps_configured(env):
        return {"code": "caps_unconfigured",
                "reason": "spend caps are not configured; live send refused"}
    payout_cap = env_int(env, "SWARMSGIT_PAYOUT_CAP_SATS")
    if payout_cap is not None and sats > payout_cap:
        return {"code": "payout_cap",
                "reason": "payout %s sats exceeds per-payout cap %s"
                          % (sats, payout_cap)}
    now = int(time.time())
    daily_cap = env_int(env, "SWARMSGIT_DAILY_CAP_SATS")
    daily = await released_sats(store, now - DAY)
    if daily_cap is not None and daily + sats > daily_cap:
        return {"code": "daily_cap",
                "reason": "daily released %s + %s exceeds cap %s"
                          % (daily, sats, daily_cap)}
    weekly_cap = env_int(env, "SWARMSGIT_WEEKLY_CAP_SATS")
    weekly = await released_sats(store, now - WEEK)
    if weekly_cap is not None and weekly + sats > weekly_cap:
        return {"code": "weekly_cap",
                "reason": "7-day released %s + %s exceeds cap %s"
                          % (weekly, sats, weekly_cap)}
    agent_cap = env_int(env, "SWARMSGIT_AGENT_DAILY_CAP_SATS")
    agent_daily = await released_sats(store, now - DAY, agent=agent)
    if agent_cap is not None and agent_daily + sats > agent_cap:
        return {"code": "agent_daily_cap",
                "reason": "agent %s daily %s + %s exceeds cap %s"
                          % (agent, agent_daily, sats, agent_cap)}
    threshold = env_int(env, "SWARMSGIT_APPROVAL_THRESHOLD_SATS")
    if threshold is not None and sats > threshold:
        status = await _approval_status(store, task_id, agent)
        if status == "approved":
            return None
        if status == "rejected":
            return {"code": "approval_rejected",
                    "reason": "operator rejected this payout"}
        if status != "pending":
            await _request_approval(store, task_id, agent, sats)
        return {"code": "needs_approval",
                "reason": "payout %s sats is above approval threshold %s"
                          % (sats, threshold)}
    return None


async def _approval_status(store, task_id, agent):
    getter = getattr(store, "get_payout_approval", None)
    if not callable(getter):
        return ""
    row = await getter(task_id, agent)
    return (row or {}).get("status") or ""


async def _request_approval(store, task_id, agent, sats):
    putter = getattr(store, "put_payout_approval", None)
    if not callable(putter):
        return
    await putter({
        "id": "appr_%s_%s" % (task_id, agent),
        "task_id": task_id, "agent": agent, "sats": int(sats),
        "status": "pending", "operator": "", "note": "",
        "created_at": int(time.time()), "decided_at": 0,
    })
    await store.ledger_add(
        "payout_approval_required", task_id, agent, int(sats),
        {"settlement": "pending", "threshold": True})


async def decide_approval(store, task_id, agent, approve, operator="", note=""):
    getter = getattr(store, "get_payout_approval", None)
    row = await getter(task_id, agent) if callable(getter) else None
    if not row or row.get("status") != "pending":
        raise RuntimeError("no pending payout approval for that task/agent")
    row = dict(row)
    row["status"] = "approved" if approve else "rejected"
    row["operator"] = (operator or "operator").strip() or "operator"
    row["note"] = (note or "")[:300]
    row["decided_at"] = int(time.time())
    await store.put_payout_approval(row)
    await store.ledger_add(
        "payout_approved" if approve else "payout_rejected",
        task_id, agent, int(row.get("sats") or 0),
        {"operator": row["operator"], "note": row["note"],
         "settlement": "dry_run"})
    return {"ok": True, "status": row["status"], "task_id": task_id,
            "agent": agent, "sats": int(row.get("sats") or 0)}


async def snapshot(env, store):
    """Board/MCP view of pause, caps, and usage. Never secrets."""
    now = int(time.time())
    paused = await is_paused(env, store)
    daily = await released_sats(store, now - DAY) if store else 0
    weekly = await released_sats(store, now - WEEK) if store else 0
    pending = []
    lister = getattr(store, "list_payout_approvals", None) if store else None
    if callable(lister):
        try:
            pending = [a for a in (await lister("pending")) or [] ]
        except Exception:
            pending = []
    missing = missing_live_requirements(env)
    return {
        "paused": paused,
        "env_paused": _truthy(env_get(env, "SWARMSGIT_PAUSED", "")),
        "live_armed": live_armed(env) and not paused,
        "live_flag": _truthy(env_get(env, "SWARMSGIT_LIVE_SETTLEMENT", "")),
        "agentpay_key_present": bool(agentpay_key(env)),
        "caps_configured": caps_configured(env),
        "missing": missing,
        "payout_cap_sats": env_int(env, "SWARMSGIT_PAYOUT_CAP_SATS"),
        "daily_cap_sats": env_int(env, "SWARMSGIT_DAILY_CAP_SATS"),
        "weekly_cap_sats": env_int(env, "SWARMSGIT_WEEKLY_CAP_SATS"),
        "agent_daily_cap_sats": env_int(env, "SWARMSGIT_AGENT_DAILY_CAP_SATS"),
        "approval_threshold_sats": env_int(env, "SWARMSGIT_APPROVAL_THRESHOLD_SATS"),
        "released_today_sats": daily,
        "released_7d_sats": weekly,
        "pending_approvals": pending,
    }


class SpendGuard:
    """Attach to deps. Production always has one; tests inject fakes."""

    def __init__(self, env):
        self.env = env

    def live_armed(self):
        return live_armed(self.env)

    async def paused(self, store):
        return await is_paused(self.env, store)

    async def gate(self, store, agent, sats, task_id=""):
        return await gate_payout(self.env, store, agent, sats, task_id)

    async def snapshot(self, store):
        return await snapshot(self.env, store)
