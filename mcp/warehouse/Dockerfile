# Combined image for the warehouse MCP:
#   - postgres-mcp runs as a stdio-transport subprocess (its native mode).
#   - supergateway exposes it over MCP Streamable HTTP on :8080, which is the
#     transport claude.ai speaks.
#
# We combine them in one container because supergateway only translates
# stdio -> (sse|ws|streamableHttp); it does NOT do sse -> streamableHttp. So
# we need postgres-mcp on stdin/stdout of supergateway.
#
# Base is python-slim (Debian) rather than node-alpine because installing a
# Python package cleanly inside Alpine's musl + pipx combo has proven fragile
# on ARM64. Adding Node on top of a Python base is straightforward.

FROM python:3.12-slim

# Node.js LTS + build tools needed for native npm deps.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# postgres-mcp from PyPI. We use a venv under /opt so it doesn't conflict
# with the system Python environment (PEP 668).
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --no-cache-dir postgres-mcp

# supergateway from npm, global install.
RUN npm install -g supergateway

EXPOSE 8080

# DATABASE_URI is read from env by postgres-mcp.
# --stdio "<cmd>" spawns postgres-mcp, supergateway proxies the JSON-RPC
# stream out on :8080 as MCP Streamable HTTP.
ENTRYPOINT ["supergateway"]
CMD [ \
  "--stdio", "postgres-mcp --access-mode=restricted", \
  "--outputTransport", "streamableHttp", \
  "--port", "8080", \
  "--streamableHttpPath", "/" \
]
