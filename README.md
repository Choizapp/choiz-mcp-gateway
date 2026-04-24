# choiz-mcp-gateway

Remote MCP gateway for the Choiz internal team. A single HTTPS URL (`https://mcp.choiz.com.mx`) that the team can add to Claude.ai (web or Desktop) as a Custom Connector, so people stop editing `claude_desktop_config.json` individually.

## Architecture

```
Claude (web or Desktop)
  -> Cloudflare Worker     (OAuth 2.0 + DCR, federates to Google Workspace)
  -> Cloudflare Tunnel     (no inbound ports opened on AWS)
  -> gateway container     (auth proxy, routes /mcp/<name> to upstream MCP)
  -> upstream MCP(s)       (each one its own Docker service)
  -> AWS VPC resources     (RDS warehouse, etc.)
```

The gateway itself does not implement MCP protocol — it is a thin authenticated reverse proxy. Each upstream MCP runs as its own service in `compose.yml`.

## Stack services

| Service | Purpose |
|---|---|
| `gateway` | Validates Worker shared secret + user email, routes `/mcp/<name>` to the right upstream. Listens on `127.0.0.1:8080`. |
| `postgres_wh_mcp` | Wraps the data warehouse. Based on public image `crystaldba/postgres-mcp` in `restricted` access mode (read-only, no schema changes). |

## Routes

| Path | Upstream | Notes |
|---|---|---|
| `POST/GET /mcp/warehouse` | `postgres_wh_mcp` | Read-only SQL access to the warehouse |
| `GET /healthz` | — | Liveness probe, no auth |

Every `/mcp/*` request must carry two headers set by the Cloudflare Worker:

- `X-Worker-Shared-Secret` — must match `WORKER_SHARED_SECRET`
- `X-Choiz-User-Email` — verified Google Workspace email, must end in `@choiz.com.mx`

Requests without both headers are rejected with `401`.

## Deployment

Runs on a dedicated EC2 (`t4g.small`, Ubuntu 24.04 ARM) in the same VPC as the RDS warehouse. Admin access is via AWS SSM Session Manager (no SSH, no VPN). Public access is via Cloudflare Tunnel.

### First-time bring-up on EC2

```bash
# 1. Clone
cd ~
git clone https://github.com/Choizapp/choiz-mcp-gateway.git
cd choiz-mcp-gateway

# 2. Create .env
cp .env.example .env
nano .env
# Fill in WORKER_SHARED_SECRET and WAREHOUSE_DATABASE_URL.

# 3. Bring the stack up
docker compose up -d --build

# 4. Smoke test locally (simulates the Worker)
curl -i http://localhost:8080/healthz
```

### Updating

```bash
cd ~/choiz-mcp-gateway
git pull
docker compose up -d --build
```

## Adding a new MCP

1. Add a service block to `compose.yml`.
2. Add a new `UPSTREAM_<NAME>` env var in the `gateway` service.
3. Add the entry to `upstreams` in [`gateway/src/index.ts`](gateway/src/index.ts).
4. Rebuild: `docker compose up -d --build`.

## Security notes

- The tunnel URL is technically reachable from the public internet; the shared secret prevents any caller other than the Cloudflare Worker from talking to the gateway.
- `postgres_wh_mcp` is **not** exposed on any host port — it only accepts connections from the `gateway` container over the internal Docker network.
- The Postgres role used by `postgres_wh_mcp` must be a read-only role. The `--access-mode=restricted` flag is belt-and-suspenders.
