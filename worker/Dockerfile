# Isolated auto-verify runner for SwarmGit.
# Bound to the VerifySandbox Durable Object (Cloudflare Containers).
# The Worker clones the fork with a short-lived read token, then execs
# acceptance tests under `unshare --net` so the tests have no egress.
# Clone and optional pip install are the only networked steps.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        util-linux \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
# Stay up so the Durable Object can exec clone + tests.
CMD ["sleep", "infinity"]
