# choiz-mcp-gateway

Remote MCP gateway for the Choiz internal team. A single HTTP service that exposes custom MCP servers over HTTPS behind Google Workspace SSO, so team members can add one URL to Claude.ai (or Claude Desktop) as a Custom Connector instead of editing `claude_desktop_config.json` individually.

## Architecture

```
Claude (web or Desktop)
  -> Cloudflare Worker     (OAuth 2.0 + DCR, federates to Google Workspace)
  -> Cloudflare Tunnel     (no inbound ports opened on AWS)
  -> this gateway          (runs in Docker on existing EC2 inside the VPC)
  -> AWS VPC resources     (RDS warehouse, future: dbt, SQLMesh, internal APIs)
```

## Endpoints

| Path | MCP |
|---|---|
| `POST/GET/DELETE /warehouse` | Read-only SQL access to the data warehouse |
| `GET /healthz` | Liveness probe |

Every request must carry two headers set by the Cloudflare Worker:

- `X-Worker-Shared-Secret`: must match `WORKER_SHARED_SECRET` env var
- `X-Choiz-User-Email`: verified email of the authenticated user (must end in `@choiz.com.mx`)

Requests that reach the gateway without both headers are rejected with `401`.

## Environment variables

See [`.env.example`](./.env.example).

## Local development

```bash
npm install
cp .env.example .env
# fill in WAREHOUSE_DATABASE_URL and WORKER_SHARED_SECRET
npm run dev
```

Smoke test (simulates the Worker):

```bash
curl -X POST http://localhost:8080/warehouse \
  -H "Content-Type: application/json" \
  -H "X-Worker-Shared-Secret: $WORKER_SHARED_SECRET" \
  -H "X-Choiz-User-Email: you@choiz.com.mx" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

## Deployment

Runs as a Docker container on the EC2 that hosts the Airflow / Airbyte pipeline. The container binds to `localhost:8080` and is reached by `cloudflared` (Cloudflare Tunnel) on the same host. No inbound ports are opened in the security group.

See [`docs/deploy.md`](./docs/deploy.md) (coming soon).

## Adding a new MCP

1. Create `src/mcps/<name>.ts` exporting `createXServer(userEmail: string): McpServer`.
2. In `src/index.ts`, add `mountMcp("/<name>", createXServer);`.
3. Optionally add to `/all` (upcoming).

## Security notes

- The gateway trusts the email header **only** when the shared secret header matches. The tunnel is not publicly reachable; only the Worker can hit it.
- The warehouse MCP enforces read-only at the query level (statement prefix check) AND at the Postgres transaction level (`SET TRANSACTION READ ONLY`).
- Every query tags `application_name = mcp:<email>` so `pg_stat_activity` and query logs are auditable per user.
