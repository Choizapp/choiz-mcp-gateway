# Adding a new MCP to the gateway

Recipe for wiring a new MCP server into the stack so team members can reach it at `https://mcp.choiz.com.mx/mcp/<name>/`.

## Step 0: classify the MCP by transport

Most MCPs you'll find in the wild fall into one of these buckets. Identify yours before choosing a Dockerfile pattern.

| Transport the MCP speaks natively | Wrap with supergateway? | Example |
|---|---|---|
| **stdio** (most common; runs as CLI subprocess) | Yes, `--stdio "<command>" --outputTransport streamableHttp` | `@modelcontextprotocol/server-*`, `mcp-server-git`, most community MCPs |
| **SSE** (HTTP server, two endpoints: `/sse` + `/messages`) | No. `supergateway` does NOT translate `sse -> streamableHttp`. Either (a) find a stdio version of the same MCP, or (b) run two containers: the SSE server + a small adapter you write yourself. | `crystaldba/postgres-mcp --transport=sse` (legacy) |
| **Streamable HTTP** (HTTP server, single endpoint) | No. Run it directly. | Newer MCPs following the 2025-03-26 spec |

If the MCP needs local filesystem access (e.g. filesystem MCP), **do not move it to the gateway** — keep it in `claude_desktop_config.json`. The gateway is for MCPs that talk to network services.

## Step 1: add a Dockerfile under `mcp/<name>/`

Pick the template that matches the transport.

### Template A — stdio MCP wrapped with supergateway

This is the most common case. See `mcp/warehouse/Dockerfile` as a full working example.

```dockerfile
FROM python:3.12-slim  # or node:20-slim if the MCP is Node-based

# --- Install the MCP server itself ---
# Python example:
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --no-cache-dir <mcp-package-name>

# Node example (alternative base):
# RUN npm install -g <mcp-package-name>

# --- Install supergateway (Streamable HTTP adapter) ---
# If your base is not Node, you need to add it first:
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN npm install -g supergateway

EXPOSE 8080

ENTRYPOINT ["supergateway"]
CMD [ \
  "--stdio", "<mcp-cli-command-with-flags>", \
  "--outputTransport", "streamableHttp", \
  "--port", "8080", \
  "--streamableHttpPath", "/" \
]
```

Critical details:

- `--streamableHttpPath /` matters: the gateway strips the `/mcp/<name>` prefix before forwarding to the upstream. If supergateway serves on its default `/mcp`, the paths won't match and you'll get 404s.
- Any credentials or connection strings the MCP needs go through environment variables in `compose.yml`, not hardcoded here.

### Template B — MCP that natively speaks Streamable HTTP

Just expose port 8080 serving at `/`. No supergateway needed.

## Step 2: add the service to `compose.yml`

```yaml
services:
  gateway:
    environment:
      # ... existing ...
      UPSTREAM_<NAME_UPPERCASE>: "http://<name>_mcp:8080"
    depends_on:
      # ... existing ...
      - <name>_mcp

  <name>_mcp:
    build: ./mcp/<name>
    environment:
      # Whatever the MCP needs. Use ${VAR:?...} so the stack fails fast if a
      # required secret is missing.
      SOME_API_KEY: ${SOME_API_KEY:?set SOME_API_KEY in .env}
    restart: unless-stopped
```

Do not expose a host port on the MCP container. It is only reachable via the gateway.

## Step 3: register the route in the gateway

Edit `gateway/src/index.ts`:

```ts
const upstreams: Record<string, string | undefined> = {
  "/mcp/warehouse": process.env.UPSTREAM_WAREHOUSE,
  "/mcp/<name>":    process.env.UPSTREAM_<NAME_UPPERCASE>,  // add this
};
```

## Step 4: add secrets to `.env.example` and `.env`

Add a commented entry to `.env.example` so the next person cloning the repo knows the new variable is required. Add the real value to the `.env` on the EC2 (over SSM).

## Step 5: rebuild the stack

On the EC2, over SSM:

```bash
cd ~/choiz-mcp-gateway
git pull
docker compose up -d --build
docker compose ps
docker compose logs <name>_mcp --tail=30
```

## Step 6: smoke test locally

```bash
curl -i -X POST http://localhost:8080/mcp/<name>/ \
  -H "x-worker-shared-secret: $(grep ^WORKER_SHARED_SECRET ~/choiz-mcp-gateway/.env | cut -d= -f2)" \
  -H "x-choiz-user-email: sabruzzini@choiz.com.mx" \
  -H "content-type: application/json" \
  -H "accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Expect `HTTP/1.1 200 OK` with a body containing `"result":{"protocolVersion":"...","serverInfo":{...}}`. If you see that, the whole backend chain works. The Worker then just proxies requests to this exact path.

## Step 7: announce the new connector

Tell the team to add a custom connector in Claude.ai with URL `https://mcp.choiz.com.mx/mcp/<name>/`. No Worker redeploy needed — the Worker proxies any `/mcp/*` path to the tunnel, so new MCPs light up automatically once the backend is ready.
