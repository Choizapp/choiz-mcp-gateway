# Warehouse MCP — postgres-mcp with native Streamable HTTP transport.
#
# History: previously this image bundled supergateway to translate stdio
# (postgres-mcp's only transport in v0.3.0) to MCP Streamable HTTP. That
# combination caused a child-process leak: each request to the gateway
# spawned a new postgres-mcp Python child, but supergateway in --stateless
# mode did not reap them on completion. Containers grew to 2.5+ GB of RAM
# after a few queries (9 zombie postgres-mcp processes × ~280 MB each),
# eventually starving the host of memory and triggering the recurring
# "tunnel zombie" pattern documented in MEMORY.md / project_tunnel_zombie_*.
#
# Fix (2026-05-06): postgres-mcp's main branch (commit 07eb329c, Jan 2026)
# added a native streamable-http transport via mcp.server.fastmcp's
# run_streamable_http_async(). This eliminates the supergateway hop entirely:
# one persistent Python process serves all requests in-band. No more child
# zombies; Python's GC reclaims per-query allocations cleanly.
#
# We pin to the exact commit 07eb329c rather than a moving branch so builds
# are reproducible. When upstream publishes 0.4.0+ on PyPI we can switch back
# to a versioned pip install.

FROM python:3.12-slim

# Just curl + ca-certs for any HTTPS deps the wheel pull may need.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Pin to the commit that introduced --transport streamable-http support.
# https://github.com/crystaldba/postgres-mcp/commit/07eb329c
RUN pip install --no-cache-dir 'git+https://github.com/crystaldba/postgres-mcp.git@07eb329c'

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

# We invoke postgres-mcp through entrypoint.py so we can monkey-patch
# FastMCP.__init__ in place to default streamable_http_path="/". The
# patch is applied to the original class (NOT a subclass) so generic
# parameterisation `FastMCP[X]` keeps working — that's the trap that
# bit us in the previous attempt.
#
# DATABASE_URI is read from env by postgres-mcp.
# --access-mode=restricted blocks DML/DDL; only SELECT/EXPLAIN are allowed.
ENTRYPOINT ["python", "/opt/entrypoint.py"]
CMD [ \
  "--transport", "streamable-http", \
  "--streamable-http-host", "0.0.0.0", \
  "--streamable-http-port", "8080", \
  "--access-mode", "restricted" \
]
