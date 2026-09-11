# Warehouse MCP — postgres-mcp on the mcp 2.x SDK (protocol revision 2026-07-28).
#
# History
# -------
# 2026-05-06: dropped supergateway for postgres-mcp's native streamable-http
# transport (commit 07eb329c), which fixed a child-process leak that grew
# containers to 2.5+ GB of RAM.
#
# 2026-08-24: an unrelated compose.yml change rebuilt all 16 images, this one
# resolved a newer mcp SDK that no longer exposed `mcp.server.fastmcp`, and the
# container crashlooped with ModuleNotFoundError. Fixed by pinning mcp==1.25.0 —
# i.e. the image had only ever been accidentally reproducible.
#
# 2026-09-11: that pin became the outage. MCP revision 2026-07-28 removed the
# initialize handshake and protocol sessions, added the mandatory
# `server/discover` RPC, and moved the protocol version into a `params._meta`
# envelope. claude.ai switched to it during the day; mcp 1.25.0 tops out at
# 2025-11-25 and answers with HTTP 400, which the client reports as
# "Connection closed" on every tool call. No 1.x release speaks 2026-07-28 —
# support exists only in 2.x, where FastMCP was renamed MCPServer. See
# mcp/warehouse/entrypoint.py for the bridge and docs/NEXT_STEPS.md for the
# fleet-wide migration status.

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# postgres-mcp is pinned to the commit that introduced streamable-http support.
# It declares `mcp[cli]>=1.25.0,<2.0`, so install it first and then force the
# 2.x SDK over the top: upstream's cap is what this image exists to bypass.
# Both versions stay pinned EXACTLY — a floating resolve is what broke this
# image on 2026-08-24.
RUN pip install --no-cache-dir \
    'git+https://github.com/crystaldba/postgres-mcp.git@07eb329c'
RUN pip install --no-cache-dir --upgrade 'mcp[cli]==2.2.0'

COPY entrypoint.py /opt/entrypoint.py

# Fail the BUILD, not production, if the SDK layout moves again. This executes
# entrypoint.py's module body (not main()), so it exercises the real bridge:
# the mcp.server.fastmcp stand-in, the transport patch, and postgres_mcp's
# import through it. A bare `import postgres_mcp` would fail here by design -
# it resolves the old FastMCP name that only the shim provides.
# Keep the check ASCII-only: the image has no locale, so printing a non-ASCII
# character raises UnicodeEncodeError and fails the build for the wrong reason.
RUN python -c "import importlib.util; from mcp_types.version import KNOWN_PROTOCOL_VERSIONS as K; assert '2026-07-28' in K, K; spec=importlib.util.spec_from_file_location('wh','/opt/entrypoint.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); assert callable(m.main); print('import check OK, protocols:', K)"

EXPOSE 8080

# entrypoint.py shims mcp.server.fastmcp -> MCPServer and supplies the
# transport kwargs (host/port/path/stateless), which in 2.x no longer live in
# Settings. DATABASE_URI is read from env by postgres-mcp.
# --access-mode=restricted blocks DML/DDL; only SELECT/EXPLAIN are allowed.
ENTRYPOINT ["python", "/opt/entrypoint.py"]
CMD [ \
  "--transport", "streamable-http", \
  "--streamable-http-host", "0.0.0.0", \
  "--streamable-http-port", "8080", \
  "--access-mode", "restricted" \
]
