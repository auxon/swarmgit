"""Artifacts adapter for SwarmGit — the ONLY module that talks to
Cloudflare Artifacts. Everything else in the worker goes through the
narrow interface below, so beta API drift touches exactly one file.

HARD CONSTRAINT (AGENTS.md `error code: 1042` lesson): Artifacts is
always reached through the Workers binding (`env.ARTIFACTS`), never via
a public-URL subfetch from the same account — the edge kills
same-account Worker-to-Worker fetches.

Binding call shapes verified 2026-10-05 against Cloudflare's docs
(Workers binding reference):
  await env.ARTIFACTS.create(name, opts?) -> {name, remote, token, ...}
  repo = await env.ARTIFACTS.get(name)          # throws if missing
  forked = await repo.fork(name, opts?)         # -> {name, remote, ...}
  token  = await repo.createToken(scope, ttl_s) # scope "read"|"write"
  await repo.readFile({"ref", "path"})          # per the launch blog;
      confirm against current docs at deploy (beta)
The Python-Workers FFI call shape (attribute vs dict access on results)
must be confirmed at deploy time; extraction below is defensive.
"""


class ArtifactsError(Exception):
    pass


def _s(v, *attrs):
    """Defensively pull a string off a JsProxy-or-dict result."""
    if v is None:
        return ""
    if isinstance(v, dict):
        for a in attrs:
            if v.get(a):
                return str(v[a])
        return ""
    for a in attrs:
        try:
            x = getattr(v, a, None)
        except Exception:
            x = None
        if x:
            return str(x() if callable(x) else x)
    return ""


class ArtifactsBackend:
    """Narrow interface. Real backend wraps env.ARTIFACTS."""

    def __init__(self, binding):
        self._b = binding

    async def create_repo(self, name, description=""):
        """Create a repo in the bound namespace.
        Returns {"repo_name", "remote", "token"}."""
        try:
            res = await self._b.create(
                name, {"description": description or "SwarmGit task repo",
                       "setDefaultBranch": "main"})
            return {"repo_name": _s(res, "name") or name,
                    "remote": _s(res, "remote"),
                    "token": _s(res, "token", "initialToken")}
        except Exception as e:
            raise ArtifactsError(f"create_repo failed: {e}")

    async def fork_repo(self, repo_name, fork_name):
        """Fork repo_name -> fork_name. Returns {"fork_id","repo_name",
        "remote"}."""
        try:
            project = await self._b.get(repo_name)
            forked = await project.fork(
                fork_name, {"description": "SwarmGit agent fork",
                            "defaultBranchOnly": True})
            return {"fork_id": "fork_" + fork_name.replace("/", "-"),
                    "repo_name": _s(forked, "name") or fork_name,
                    "remote": _s(forked, "remote")}
        except Exception as e:
            raise ArtifactsError(f"fork_repo failed: {e}")

    async def read_file(self, repo_name, ref, path):
        """Read a file at a ref. Returns text or None. Call shape per
        the launch blog — re-verify against current docs at deploy."""
        try:
            repo = await self._b.get(repo_name)
            f = await repo.readFile({"ref": ref, "path": path})
            if f is None:
                return None
            text = f.text() if hasattr(f, "text") else f
            if isinstance(text, dict):
                text = text.get("text", "")
            return str(text) if text is not None else None
        except Exception as e:
            raise ArtifactsError(f"read_file failed: {e}")

    async def issue_token(self, repo_name, ttl_s=86400, scope="write"):
        """Issue a short-lived repo-scoped Git token. Agents get
        "write" (they must push); verifiers/auditors get "read"."""
        try:
            repo = await self._b.get(repo_name)
            tok = await repo.createToken(scope, int(ttl_s))
            return _s(tok, "token") or str(tok)
        except Exception as e:
            raise ArtifactsError(f"issue_token failed: {e}")


def from_env(env):
    """Build the real backend from the worker env. Raises if the
    ARTIFACTS binding is missing (fail closed, like bearer auth).
    A pre-built ArtifactsBackend (e.g. FakeArtifacts in tests) passes
    through unwrapped."""
    binding = getattr(env, "ARTIFACTS", None)
    if binding is None:
        raise ArtifactsError("ARTIFACTS binding not configured")
    if isinstance(binding, ArtifactsBackend):
        return binding
    return ArtifactsBackend(binding)


class FakeArtifacts(ArtifactsBackend):
    """In-memory Artifacts stand-in for tests and local rehearsal.
    Same interface, no network, no account."""

    def __init__(self):
        super().__init__(binding=None)
        self.repos = {}   # name -> {"files": {path: text}, "commits": [...]}

    async def create_repo(self, name):
        if name in self.repos:
            raise ArtifactsError(f"repo exists: {name}")
        self.repos[name] = {"files": {}, "commits": []}
        return {"repo_name": name}

    async def fork_repo(self, repo_name, fork_name):
        src = self.repos.get(repo_name)
        if src is None:
            raise ArtifactsError(f"repo not found: {repo_name}")
        if fork_name in self.repos:
            raise ArtifactsError(f"repo exists: {fork_name}")
        self.repos[fork_name] = {
            "files": dict(src["files"]),
            "commits": list(src["commits"]),
        }
        return {"fork_id": "fork_" + fork_name.replace("/", "-"),
                "repo_name": fork_name}

    async def read_file(self, repo_name, ref, path):
        repo = self.repos.get(repo_name)
        if repo is None:
            raise ArtifactsError(f"repo not found: {repo_name}")
        return repo["files"].get(path)

    async def issue_token(self, repo_name, ttl_s=3600):
        if repo_name not in self.repos:
            raise ArtifactsError(f"repo not found: {repo_name}")
        return "fake-token-" + repo_name[-8:]

    # -- test helpers (not part of the narrow interface) ------------
    def write_file(self, repo_name, path, text):
        self.repos[repo_name]["files"][path] = text

    def push_event(self, repo_name, ref="main", commit="c0ffee"):
        """Shape of the queue message a real push subscription delivers."""
        return {"kind": "artifact_push",
                "event": {"type": "cf.artifacts.repo.pushed",
                          "source": {"repoName": repo_name},
                          "payload": {"ref": ref, "after": commit}}}
