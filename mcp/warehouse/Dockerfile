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
#
# `mcp` is pinned EXPLICITLY even though postgres-mcp pulls it transitively.
# Outage 2026-08-24: an unrelated compose.yml change rebuilt all 16 images, this
# one resolved a newer mcp SDK that no longer exposes `mcp.server.fastmcp`, and
# the container crashlooped with ModuleNotFoundError. The old container had been
# running fine for months on a version resolved at its original build — i.e. the
# image was only accidentally reproducible. 1.25.0 matches the rest of the stack
# (dhl / powerbi / gmail / instagram / facebook / tiktok-organic / viral-loops).
RUN pip install --no-cache-dir \
    'git+https://github.com/crystaldba/postgres-mcp.git@07eb329c' \
    'mcp==1.25.0'

# Fail the BUILD, not production, if the SDK layout entrypoint.py monkey-patches
# ever moves again. This is the exact import that broke on 2026-08-24; a
# crashlooping container discovered by a user is the expensive way to learn it.
RUN python -c "import mcp.server.fastmcp; import postgres_mcp; print('import check OK')"

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
