# Power BI MCP — custom server, no upstream package to wrap.
#
# Talks to Power BI Service via the REST API endpoint
# /datasets/{id}/executeQueries with a Service Principal (client_credentials).
# This means the auth is fully server-side: end users only authenticate to
# claude.ai with Google Workspace; they never see a Microsoft login prompt.
#
# Why REST + SP instead of XMLA:
#   - PPU includes executeQueries with SP support (no RLS/SSO on our models —
#     verified via POC 2026-05-19, see project_powerbi_mcp_poc_2026-05-19).
#   - Pure HTTP — no ADOMD.NET / .NET runtime needed on ARM64 Linux.
#   - Container stays slim (~100 MB resident, same shape as GA4/GSC).
#   - Limits (100K rows / 1M values / 120 qpm) are fine for LLM-driven Q&A.
#
# Single container, multi-dataset by slug: PBI_DATASET_CHOIZ + PBI_DATASET_TIMELESS
# map to the "choiz" / "timeless" slug arg on each tool. This diverges from
# the per-container per-tenant convention (ga4-choiz / ga4-timeless) because
# both semantic models live under the SAME service principal in the SAME
# workspace; spinning up two identical containers would only add memory cost
# with no separation benefit.

FROM python:3.12-slim

# ca-certificates: HTTPS to login.microsoftonline.com + api.powerbi.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# mcp 2.x is REQUIRED, not a nice-to-have: protocol revision 2026-07-28 (which
# claude.ai switched to on 2026-09-11) removed the initialize handshake and
# added the mandatory server/discover RPC. Every 1.x release tops out at
# 2025-11-25 and answers the new revision with a bare HTTP 400, which the
# client reports as "Connection closed" on every tool call. 2.x renamed
# FastMCP to MCPServer — see entrypoint.py. Pinned EXACTLY: a floating resolve
# is what broke this stack on 2026-08-24.
# msal handles token caching + silent refresh in-process so the SP token
# re-mints transparently before the 1-hour Azure AD TTL.
RUN pip install --no-cache-dir \
    'mcp==2.2.0' \
    'msal>=1.28,<2' \
    'requests>=2.31,<3'

ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

# Fail the BUILD, not production, if the SDK layout moves again. We can NOT
# exec entrypoint.py's module body here the way warehouse does: this module
# builds an MSAL client at import time, which validates the tenant against
# login.microsoftonline.com and dies on a dummy value. So assert the two
# things that actually broke before — the protocol revision and the exact
# transport kwargs we pass to run() — plus a syntax check of the file.
RUN python -c "\
import inspect, py_compile, sys; \
from mcp.server.mcpserver import MCPServer; \
from mcp_types.version import KNOWN_PROTOCOL_VERSIONS as K; \
assert '2026-07-28' in K, K; \
p = inspect.signature(MCPServer.run_streamable_http_async).parameters; \
missing = [k for k in ('host','port','streamable_http_path','stateless_http') if k not in p]; \
sys.exit('run_streamable_http_async lost kwargs: %s' % missing) if missing else None; \
py_compile.compile('/opt/entrypoint.py', doraise=True); \
print('import check OK, protocols:', K)"

EXPOSE 8080

ENTRYPOINT ["python", "/opt/entrypoint.py"]
