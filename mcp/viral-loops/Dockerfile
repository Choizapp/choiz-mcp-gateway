# Viral Loops MCP — custom server, no upstream package to wrap.
#
# Talks to the Viral Loops Web API v3 (app.viral-loops.com/api/v3) over HTTPS
# with a per-campaign `apiToken` header. End users authenticate to claude.ai
# with Google Workspace; they never see a Viral Loops login.
#
# READ-ONLY: only GET endpoints are exposed (campaign info/stats + participant
# data/referrals/rank/order/referrer/rewards). Mirrors the dhl / tiktok-organic
# shape: FastMCP + requests, stateless_http=True, no SDK.

FROM python:3.12-slim

# ca-certificates: HTTPS to app.viral-loops.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin mcp to 1.25.0 to match the rest of the stack (warehouse / facebook /
# instagram / ga4 / powerbi / tiktok-organic / dhl). requests for the HTTP calls.
# mcp 2.x is REQUIRED, not a nice-to-have: protocol revision 2026-07-28 (which
# claude.ai switched to on 2026-09-11) removed the initialize handshake and
# added the mandatory server/discover RPC. Every 1.x release tops out at
# 2025-11-25 and answers the new revision with a bare HTTP 400, which the
# client reports as "Connection closed" on every tool call. 2.x renamed
# FastMCP to MCPServer — see entrypoint.py. Pinned EXACTLY: a floating resolve
# is what broke this stack on 2026-08-24.
RUN pip install --no-cache-dir \
    'mcp==2.2.0' \
    'requests>=2.31,<3'

ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

# Fail the BUILD, not production, if the SDK layout moves again. Asserts the
# protocol revision and the exact transport kwargs run() forwards, plus a
# syntax check of the entrypoint. (We do not exec the module body: these
# entrypoints read required env vars and build API clients at import time.)
RUN python -c "import inspect, py_compile, sys; from mcp.server.mcpserver import MCPServer; from mcp_types.version import KNOWN_PROTOCOL_VERSIONS as K; assert '2026-07-28' in K, K; p = inspect.signature(MCPServer.run_streamable_http_async).parameters; missing = [k for k in ('host','port','streamable_http_path','stateless_http') if k not in p]; sys.exit('run_streamable_http_async lost kwargs: %s' % missing) if missing else None; py_compile.compile('/opt/entrypoint.py', doraise=True); print('import check OK, protocols:', K)"


EXPOSE 8080

ENTRYPOINT ["python", "/opt/entrypoint.py"]
