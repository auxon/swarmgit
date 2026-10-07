"""D1-backed board storage for the SwarmGit Worker.

Forked from the TestSwarm scaffold's store.py. Same low-level plumbing
(JsProxy defensiveness, no-None binds, idempotency, rate limiting); all
domain tables are SwarmGit's: coding tasks, claims, forks, verifications,
merges, dry-run escrow ledger, reputation, why-graph staging.
"""
import json
import time


def _py(v):
    to_py = getattr(v, "to_py", None)
    return to_py() if callable(to_py) else v


def _row(r):
    r = _py(r)
    return dict(r) if not isinstance(r, dict) else r


_DDL = [
    """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  repo TEXT NOT NULL,
  bounty_sats INTEGER NOT NULL,
  poster TEXT NOT NULL DEFAULT 'anon',
  data TEXT NOT NULL,
  created_at INTEGER NOT NULL
)
    """,
    "CREATE INDEX IF NOT EXISTS idx_sgtasks_status ON tasks(status)",
    """
CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  status TEXT NOT NULL,
  data TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(task_id, agent)
)
    """,
    "CREATE INDEX IF NOT EXISTS idx_sgclaims_task ON claims(task_id)",
    """
CREATE TABLE IF NOT EXISTS forks (
  fork_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  repo_name TEXT NOT NULL,
  preview_url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  data TEXT NOT NULL,
  created_at INTEGER NOT NULL
)
    """,
    "CREATE INDEX IF NOT EXISTS idx_sgforks_task ON forks(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_sgforks_repo ON forks(repo_name)",
    """
CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fork_id TEXT NOT NULL,
  verifier TEXT NOT NULL,
  verdict TEXT NOT NULL,
  stake_sats INTEGER NOT NULL,
  repro TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL,
  created_at INTEGER NOT NULL
)
    """,
    "CREATE INDEX IF NOT EXISTS idx_sgverifs_fork ON verifications(fork_id)",
    """
CREATE TABLE IF NOT EXISTS merges (
  merge_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  fork_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  audit_report TEXT NOT NULL,
  why_refs TEXT NOT NULL,
  data TEXT NOT NULL,
  merged_at INTEGER NOT NULL
)
    """,
    "CREATE INDEX IF NOT EXISTS idx_sgmerges_task ON merges(task_id)",
    """
CREATE TABLE IF NOT EXISTS escrow_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t INTEGER NOT NULL,
  kind TEXT NOT NULL,
  task_id TEXT NOT NULL,
  agent TEXT NOT NULL DEFAULT '',
  sats INTEGER NOT NULL,
  settlement TEXT NOT NULL,
  detail TEXT NOT NULL
)
    """,
    "CREATE INDEX IF NOT EXISTS idx_sgescrow_task ON escrow_ledger(task_id)",
    """
CREATE TABLE IF NOT EXISTS reputation (
  agent TEXT PRIMARY KEY,
  score INTEGER NOT NULL DEFAULT 0,
  tasks_won INTEGER NOT NULL DEFAULT 0,
  tasks_verified INTEGER NOT NULL DEFAULT 0,
  false_reports INTEGER NOT NULL DEFAULT 0
)
    """,
    """
CREATE TABLE IF NOT EXISTS why_entries (
  id TEXT PRIMARY KEY,
  merge_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  entry TEXT NOT NULL,
  created_at INTEGER NOT NULL
)
    """,
    "CREATE INDEX IF NOT EXISTS idx_sgwhy_merge ON why_entries(merge_id)",
    """
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  result TEXT NOT NULL,
  fingerprint TEXT,
  at INTEGER NOT NULL
)
    """,
    """
CREATE TABLE IF NOT EXISTS post_rate (
  host TEXT NOT NULL,
  at INTEGER NOT NULL
)
    """,
    "CREATE INDEX IF NOT EXISTS idx_sgpost_rate_host ON post_rate(host, at)",
    """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  sub TEXT NOT NULL,
  email TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
)
    """,
    "CREATE INDEX IF NOT EXISTS idx_sgsessions_exp ON sessions(expires_at)",
]


def _check_no_none(args):
    # workerd's Python FFI converts None -> undefined, which D1 rejects.
    # Fail loudly here instead of surfacing a cryptic JsException.
    if any(a is None for a in args):
        raise TypeError(
            "D1 bind does not accept None (Python FFI turns it into "
            "undefined, which D1 rejects); use a NULL literal in the SQL "
            "or a sentinel value")


class D1Store:
    def __init__(self, db):
        self.db = db

    async def ensure_schema(self):
        """Create tables on first use. DDL mirrors src/schema.sql
        (test/test_local.py asserts parity)."""
        for stmt in _DDL:
            await self._run(stmt)

    # -- low-level -------------------------------------------------
    async def _all(self, sql, *args):
        _check_no_none(args)
        res = _py(await self.db.prepare(sql).bind(*args).all())
        rows = res["results"] if isinstance(res, dict) else res.results
        return [_row(r) for r in _py(rows)]

    async def _first(self, sql, *args):
        rows = await self._all(sql, *args)
        return rows[0] if rows else None

    async def _run(self, sql, *args):
        _check_no_none(args)
        await self.db.prepare(sql).bind(*args).run()

    async def _changes(self, sql, *args):
        _check_no_none(args)
        res = _py(await self.db.prepare(sql).bind(*args).run())
        meta = res.get("meta", {}) if isinstance(res, dict) else res.meta
        meta = _py(meta)
        meta = dict(meta) if not isinstance(meta, dict) else meta
        return int(meta.get("changes", 0))

    # -- tasks -----------------------------------------------------
    async def get_task(self, task_id):
        r = await self._first("SELECT data FROM tasks WHERE task_id = ?",
                              task_id)
        return json.loads(r["data"]) if r else None

    async def put_task(self, task):
        await self._run(
            "INSERT INTO tasks (task_id, status, repo, bounty_sats, poster,"
            " data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,"
            " data=excluded.data",
            task["task_id"], task.get("status", "open"),
            task.get("repo", ""), int(task.get("bounty_sats", 0)),
            task.get("poster", "anon"), json.dumps(task),
            int(task.get("created_at", time.time())))

    async def list_tasks(self, status=None):
        if status:
            rows = await self._all(
                "SELECT data FROM tasks WHERE status = ?"
                " ORDER BY created_at", status)
        else:
            rows = await self._all(
                "SELECT data FROM tasks ORDER BY created_at")
        return [json.loads(r["data"]) for r in rows]

    # -- claims ----------------------------------------------------
    async def get_claim(self, claim_id):
        r = await self._first("SELECT data FROM claims WHERE id = ?",
                              claim_id)
        return json.loads(r["data"]) if r else None

    async def put_claim(self, claim):
        # UNIQUE(task_id, agent) enforced by DDL; IntegrityError surfaces
        # as an exception the caller converts to a clean refusal.
        await self._run(
            "INSERT INTO claims (id, task_id, agent, status, data,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
            " data=excluded.data",
            claim["id"], claim["task_id"], claim["agent"],
            claim.get("status", "active"), json.dumps(claim),
            int(claim.get("created_at", time.time())))

    async def claims_for_task(self, task_id):
        rows = await self._all(
            "SELECT data FROM claims WHERE task_id = ? ORDER BY created_at",
            task_id)
        return [json.loads(r["data"]) for r in rows]

    async def claim_exists(self, task_id, agent):
        r = await self._first(
            "SELECT 1 AS x FROM claims WHERE task_id = ? AND agent = ?",
            task_id, agent)
        return r is not None

    # -- forks -----------------------------------------------------
    async def get_fork(self, fork_id):
        r = await self._first("SELECT data FROM forks WHERE fork_id = ?",
                              fork_id)
        return json.loads(r["data"]) if r else None

    async def put_fork(self, fork):
        await self._run(
            "INSERT INTO forks (fork_id, task_id, claim_id, repo_name,"
            " preview_url, status, data, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(fork_id) DO UPDATE SET status=excluded.status,"
            " preview_url=excluded.preview_url, data=excluded.data",
            fork["fork_id"], fork["task_id"], fork["claim_id"],
            fork.get("repo_name", ""), fork.get("preview_url", ""),
            fork.get("status", "working"), json.dumps(fork),
            int(fork.get("created_at", time.time())))

    async def forks_for_task(self, task_id):
        rows = await self._all(
            "SELECT data FROM forks WHERE task_id = ? ORDER BY created_at",
            task_id)
        return [json.loads(r["data"]) for r in rows]

    async def fork_by_repo(self, repo_name):
        r = await self._first(
            "SELECT data FROM forks WHERE repo_name = ? ORDER BY created_at"
            " DESC LIMIT 1", repo_name)
        return json.loads(r["data"]) if r else None

    # -- verifications ---------------------------------------------
    async def add_verification(self, verif):
        await self._run(
            "INSERT INTO verifications (fork_id, verifier, verdict,"
            " stake_sats, repro, data, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            verif["fork_id"], verif["verifier"], verif["verdict"],
            int(verif.get("stake_sats", 0)), verif.get("repro", ""),
            json.dumps(verif), int(verif.get("created_at", time.time())))

    async def verifications_for_fork(self, fork_id):
        rows = await self._all(
            "SELECT verifier, verdict, stake_sats, repro, data, created_at"
            " FROM verifications WHERE fork_id = ? ORDER BY id", fork_id)
        out = []
        for r in rows:
            v = json.loads(r["data"])
            v["verifier"] = r["verifier"]
            v["verdict"] = r["verdict"]
            out.append(v)
        return out

    async def verifier_attested(self, fork_id, verifier):
        r = await self._first(
            "SELECT 1 AS x FROM verifications WHERE fork_id = ?"
            " AND verifier = ?", fork_id, verifier)
        return r is not None

    # -- merges ----------------------------------------------------
    async def put_merge(self, merge):
        await self._run(
            "INSERT INTO merges (merge_id, task_id, fork_id, decision,"
            " audit_report, why_refs, data, merged_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(merge_id) DO UPDATE SET decision=excluded.decision,"
            " data=excluded.data",
            merge["merge_id"], merge["task_id"], merge["fork_id"],
            merge.get("decision", "merged"),
            json.dumps(merge.get("audit_report", {})),
            json.dumps(merge.get("why_refs", [])),
            json.dumps(merge), int(merge.get("merged_at", time.time())))

    async def merges_for_task(self, task_id):
        rows = await self._all(
            "SELECT merge_id, task_id, fork_id, decision, audit_report,"
            " why_refs, data, merged_at FROM merges WHERE task_id = ?"
            " ORDER BY merged_at", task_id)
        out = []
        for r in rows:
            m = json.loads(r["data"])
            m["audit_report"] = json.loads(r["audit_report"])
            m["why_refs"] = json.loads(r["why_refs"])
            out.append(m)
        return out

    # -- escrow ledger (dry-run only) -------------------------------
    async def ledger_add(self, kind, task_id, agent, sats, detail):
        await self._run(
            "INSERT INTO escrow_ledger (t, kind, task_id, agent, sats,"
            " settlement, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            int(time.time()), kind, task_id, agent or "", int(sats),
            "dry_run", json.dumps(detail or {}))

    async def ledger_for_task(self, task_id):
        rows = await self._all(
            "SELECT t, kind, agent, sats, settlement, detail"
            " FROM escrow_ledger WHERE task_id = ? ORDER BY id", task_id)
        return [{"t": r["t"], "kind": r["kind"], "agent": r["agent"],
                 "sats": int(r["sats"]), "settlement": r["settlement"],
                 "detail": json.loads(r["detail"])} for r in rows]

    # -- reputation -------------------------------------------------
    async def get_reputation(self, agent):
        r = await self._first(
            "SELECT score, tasks_won, tasks_verified, false_reports"
            " FROM reputation WHERE agent = ?", agent)
        if r:
            return {"score": int(r["score"]),
                    "tasks_won": int(r["tasks_won"]),
                    "tasks_verified": int(r["tasks_verified"]),
                    "false_reports": int(r["false_reports"])}
        return {"score": 0, "tasks_won": 0, "tasks_verified": 0,
                "false_reports": 0}

    async def bump_reputation(self, agent, **deltas):
        cur = await self.get_reputation(agent)
        for k, v in deltas.items():
            cur[k] = int(cur.get(k, 0)) + int(v)
        await self._run(
            "INSERT INTO reputation (agent, score, tasks_won,"
            " tasks_verified, false_reports) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(agent) DO UPDATE SET score=excluded.score,"
            " tasks_won=excluded.tasks_won,"
            " tasks_verified=excluded.tasks_verified,"
            " false_reports=excluded.false_reports",
            agent, cur["score"], cur["tasks_won"], cur["tasks_verified"],
            cur["false_reports"])

    async def leaderboard(self, limit=25):
        rows = await self._all(
            "SELECT agent, score, tasks_won, tasks_verified, false_reports"
            " FROM reputation ORDER BY score DESC LIMIT ?", int(limit))
        return [{"agent": r["agent"], "score": int(r["score"]),
                 "tasks_won": int(r["tasks_won"]),
                 "tasks_verified": int(r["tasks_verified"]),
                 "false_reports": int(r["false_reports"])} for r in rows]

    # -- why-graph staging ------------------------------------------
    async def stage_why_entry(self, entry):
        await self._run(
            "INSERT INTO why_entries (id, merge_id, kind, entry, created_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET entry=excluded.entry",
            entry["id"], entry["merge_id"], entry["kind"],
            json.dumps(entry), int(entry.get("created_at", time.time())))

    async def why_entries_for_merge(self, merge_id):
        rows = await self._all(
            "SELECT entry FROM why_entries WHERE merge_id = ?"
            " ORDER BY created_at", merge_id)
        return [json.loads(r["entry"]) for r in rows]

    # -- idempotency ------------------------------------------------
    async def idem_get(self, key):
        r = await self._first(
            "SELECT tool, result, fingerprint FROM idempotency WHERE key = ?",
            key)
        if not r:
            return None
        return {"tool": r["tool"], "result": json.loads(r["result"]),
                "fingerprint": r["fingerprint"] or None}

    async def idem_put(self, key, tool, result, fingerprint=None):
        fp = "" if fingerprint is None else fingerprint
        await self._run(
            "INSERT INTO idempotency (key, tool, result, fingerprint, at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET tool=excluded.tool,"
            " result=excluded.result, fingerprint=excluded.fingerprint,"
            " at=excluded.at",
            key, tool, json.dumps(result), fp, int(time.time()))
        await self._run("DELETE FROM idempotency WHERE at < ?",
                        int(time.time()) - 7 * 86400)

    # -- sessions (Google sign-in) ---------------------------------
    async def session_put(self, s):
        await self._run(
            "INSERT INTO sessions (session_id, sub, email, name,"
            " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            " sub=excluded.sub, email=excluded.email, name=excluded.name,"
            " created_at=excluded.created_at,"
            " expires_at=excluded.expires_at",
            s["session_id"], s["sub"], s.get("email", ""),
            s.get("name", ""), int(s["created_at"]),
            int(s["expires_at"]))

    async def session_get(self, session_id):
        return await self._first(
            "SELECT session_id, sub, email, name, created_at, expires_at"
            " FROM sessions WHERE session_id = ?", session_id)

    async def session_delete(self, session_id):
        await self._run("DELETE FROM sessions WHERE session_id = ?",
                        session_id)

    # -- rate limiting ----------------------------------------------
    async def rate_allow(self, key, limit=5, window=3600):
        """True if another post for key is allowed (and records it)."""
        now = int(time.time())
        await self._run("DELETE FROM post_rate WHERE at < ?", now - window)
        rows = await self._all(
            "SELECT COUNT(*) AS n FROM post_rate WHERE host = ?", key)
        if int(rows[0]["n"]) >= limit:
            return False
        await self._run("INSERT INTO post_rate (host, at) VALUES (?, ?)",
                        key, now)
        return True
