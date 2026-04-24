# Combined image for the warehouse MCP:
#   - postgres-mcp runs as a stdio-transport subprocess (its native mode).
#   - supergateway exposes it over MCP Streamable HTTP on :8080, which is the
#     transport claude.ai speaks.
#
# We combine them in one container because supergateway only translates
# stdio -> (sse|ws|streamableHttp); it does NOT do sse -> streamableHttp. So
# we need postgres-mcp on stdin/stdout of supergateway.

FROM node:20-alpine

# Python + pipx so postgres-mcp's CLI ends up on PATH in a venv.
RUN apk add --no-cache python3 py3-pip pipx \
    && pipx ensurepath \
    && pipx install postgres-mcp
ENV PATH="/root/.local/bin:${PATH}"

# supergateway is published to npm.
RUN npm install -g supergateway

EXPOSE 8080

# DATABASE_URI is read from env by postgres-mcp.
# --stdio "<cmd>" spawns postgres-mcp, supergateway proxies the JSON-RPC
# stream out on :8080 as MCP Streamable HTTP.
ENTRYPOINT ["supergateway"]
CMD [ \
  "--stdio", "postgres-mcp --access-mode=restricted", \
  "--outputTransport", "streamableHttp", \
  "--port", "8080" \
]
