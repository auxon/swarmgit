"""Hosts the TestSwarm connector will never test.

Exact match or subdomain-suffix match. Ported from
../mcp_server/blocklist.json. Edit freely and redeploy.
"""
BLOCKED_HOSTS = [
    "169.254.169.254",          # cloud instance metadata (all clouds)
    "metadata.google.internal",  # GCP metadata
    "metadata.google",           # GCP metadata
]
