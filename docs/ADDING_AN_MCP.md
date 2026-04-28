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

### Known failure modes worth pre-empting

- **Primitive return types in `mcp[cli]` 1.27 (FastMCP)** — a tool annotated `-> int | bool | float` and returning the raw value triggers `outputSchema` auto-generation; claude.ai's validator rejects the result. Patch the fork to annotate `-> str` and wrap the return in `str(...)`. Surfaced on facebook MCP, fix lives in `Choizapp/choiz-facebook-mcp@6418c71`.
- **Tool-result payload ceiling (~2-3 KB)** — claude.ai (or some hop in Worker→Tunnel→client) rejects streamable-http tool results larger than ~2-3 KB with a generic "Error occurred during tool execution", no server-side trace, while curl-against-the-gateway still returns 200 OK. Fixes seen so far in forks:
  - Avoid pretty-printed JSON in TextContent (`json.dumps(...)` not `indent=2`).
  - Strip query strings from CDN URLs (Facebook/Instagram CDN URLs carry 400-500 chars of signed auth params per asset that expire in hours). Surfaced on instagram MCP, fix in `Choizapp/choiz-instagram-mcp@1c01c4d`.
  - For protobuf-based MCPs: `MessageToDict(..., including_default_value_fields=True)` dumps the entire schema (~80 fields per row for Google Ads campaign), inflating multi-row results well past the ceiling. Parse the user's query (e.g. GAQL SELECT clause) and filter output to those fields only — keeps zeros in requested metrics, drops everything else. See `Choizapp/choiz-google-ads-mcp@6fefe68`.
  - Default ASCII-padded "table" output is a multi-x amplifier on whitespace; default to compact CSV/JSON unless the user explicitly asks for "table".
- **`supergateway --stateless` respawns the python child per request** — module-level caches (e.g. a singleton API client) are no-op under this mode. Each call re-imports everything and drives ~50-90 MB of RSS growth per call on Python MCPs with heavy SDKs (gRPC, google-ads). Mitigations: (a) set `mem_limit` + `pids_limit` on the service from day one so blow-ups stay contained to the container with `restart: unless-stopped` recovery; (b) consider moving to stateful supergateway or to native FastMCP HTTP transport once a real fix is needed across multiple MCPs. Surfaced on google-ads MCP.

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

## Step 2: add the service to `compose.yml` and `compose.dev.yml`

In `compose.yml` (production — pulls from GHCR):

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
    image: ghcr.io/choizapp/choiz-mcp-gateway/<name>:${IMAGE_TAG:-latest}
    environment:
      # Whatever the MCP needs. Use ${VAR:?...} so the stack fails fast if a
      # required secret is missing.
      SOME_API_KEY: ${SOME_API_KEY:?set SOME_API_KEY in .env}
    restart: unless-stopped
```

In `compose.dev.yml` (local builds for dev):

```yaml
services:
  <name>_mcp:
    build: ./mcp/<name>
    image: ghcr.io/choizapp/choiz-mcp-gateway/<name>:dev
```

Do not expose a host port on the MCP container. It is only reachable via the gateway.

### Multi-tenant variant (one image, N containers)

Some MCPs need to serve multiple Choiz tenants (e.g. `facebook-choiz` vs
`facebook-timeless`, `ga4-choiz` vs `ga4-timeless`). The agreed pattern is
**one image, N service blocks**, distinguished only by env vars and route
slug. Worked example from `mcp/facebook/`:

```yaml
# Same image for both — differ only in env (token + page id).
facebook_choiz_mcp:
  image: ghcr.io/choizapp/choiz-mcp-gateway/facebook:${IMAGE_TAG:-latest}
  environment:
    FACEBOOK_ACCESS_TOKEN: ${FACEBOOK_CHOIZ_ACCESS_TOKEN:?...}
    FACEBOOK_PAGE_ID:      ${FACEBOOK_CHOIZ_PAGE_ID:?...}
  restart: unless-stopped

facebook_timeless_mcp:
  image: ghcr.io/choizapp/choiz-mcp-gateway/facebook:${IMAGE_TAG:-latest}
  environment:
    FACEBOOK_ACCESS_TOKEN: ${FACEBOOK_TIMELESS_ACCESS_TOKEN:?...}
    FACEBOOK_PAGE_ID:      ${FACEBOOK_TIMELESS_PAGE_ID:?...}
  restart: unless-stopped
```

In the gateway block: add **one upstream env per tenant** and one
`depends_on` entry per tenant. In `gateway/src/index.ts`: add **one route
per tenant** (`/mcp/<name>-<tenant>/`). In CI: still **one** `build-<name>`
job — both containers consume the same image. In `.env`: **N copies of
each secret**, prefixed by tenant (e.g. `FACEBOOK_CHOIZ_*`,
`FACEBOOK_TIMELESS_*`).

Naming convention: service `<name>_<tenant>_mcp`, slug `/mcp/<name>-<tenant>/`,
env prefix `<NAME>_<TENANT>_*`.

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

## Step 5: add a build job to the CI/CD workflow

Edit `.github/workflows/deploy-gateway.yml`. Add a `<name>` filter to the `changes` job and a new `build-<name>` job mirroring the existing three.

In the `changes` job's `filters:` block:

```yaml
<name>:
  - 'mcp/<name>/**'
```

Add it to the `outputs:` of the `changes` job too.

Then a new build job:

```yaml
build-<name>:
  name: Build <name> image
  needs: changes
  if: needs.changes.outputs.<name> == 'true' || needs.changes.outputs.compose == 'true' || github.event_name == 'workflow_dispatch'
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: docker/setup-qemu-action@v3
    - uses: docker/setup-buildx-action@v3
    - name: Log in to GHCR
      uses: docker/login-action@v3
      with:
        registry: ghcr.io
        username: ${{ github.actor }}
        password: ${{ secrets.GITHUB_TOKEN }}
    - name: Build and push
      uses: docker/build-push-action@v6
      with:
        context: ./mcp/<name>
        push: true
        platforms: linux/arm64
        tags: |
          ${{ env.IMAGE_PREFIX }}/<name>:latest
          ${{ env.IMAGE_PREFIX }}/<name>:${{ github.sha }}
        cache-from: type=gha,scope=<name>
        cache-to: type=gha,mode=max,scope=<name>
```

Add `build-<name>` to the `needs:` array of the `deploy` job.

## Step 6: deploy

```bash
git push origin master
```

GitHub Actions runs the build, pushes the image to GHCR, and deploys via SSM. Watch at https://github.com/Choizapp/choiz-mcp-gateway/actions. Total time: ~5-15 min depending on the new image's size and complexity (Node MCPs under QEMU are the slowest).

After deploy, verify:

```bash
# On EC2 over SSM:
sudo docker compose -f /home/ssm-user/choiz-mcp-gateway/compose.yml ps
sudo docker compose -f /home/ssm-user/choiz-mcp-gateway/compose.yml logs <name>_mcp --tail=30
```

## Step 7: smoke test from EC2

Over SSM, hit the gateway directly:

```bash
curl -i -X POST http://localhost:8080/mcp/<name>/ \
  -H "x-worker-shared-secret: $(grep ^WORKER_SHARED_SECRET /home/ssm-user/choiz-mcp-gateway/.env | cut -d= -f2)" \
  -H "x-choiz-user-email: sabruzzini@choiz.com.mx" \
  -H "content-type: application/json" \
  -H "accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Expect `HTTP/1.1 200 OK` with a body containing `"result":{"protocolVersion":"...","serverInfo":{...}}`. If you see that, the whole backend chain works. The Worker then just proxies requests to this exact path.

## Step 8: announce the new connector

Tell the team to add a custom connector in Claude.ai with URL `https://mcp.choiz.com.mx/mcp/<name>/`. No Worker redeploy needed — the Worker proxies any `/mcp/*` path to the tunnel, so new MCPs light up automatically once the backend is ready.
