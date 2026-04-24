# choiz-mcp-gateway

Remote MCP gateway for the Choiz internal team. A single HTTPS URL per MCP server (under `mcp.choiz.com.mx`) that team members add to Claude.ai as a Custom Connector, so nobody has to edit `claude_desktop_config.json` by hand or run MCP servers locally.

## Architecture

```
Claude.ai (web or Desktop)
  │  OAuth 2.0 + DCR
  ▼
Cloudflare Worker        (mcp.choiz.com.mx/*)
  │  federates to Google Workspace; restricts to @choiz.com.mx
  │  signs upstream requests with WORKER_SHARED_SECRET + user email
  ▼
Cloudflare Tunnel        (tunnel.choiz.com.mx)
  │  outbound-only, no inbound ports on EC2
  ▼
Gateway container        (127.0.0.1:8080 on EC2)
  │  validates shared secret + user email header
  │  routes /mcp/<name> to the matching upstream
  ▼
MCP container(s)         (one per MCP, internal Docker network)
  │  speaks MCP Streamable HTTP (the transport Claude.ai uses)
  │  if the underlying server only speaks stdio or SSE, supergateway
  │  is bundled in the same image to translate
  ▼
Backend resources        (RDS warehouse, etc.)
```

Design principles:

- The gateway itself does not speak MCP. It is a thin authenticated reverse proxy.
- Each MCP is its own container, reachable at `/mcp/<name>/`.
- The Worker is the only component that holds Google credentials; the gateway only sees a shared secret + a verified email string.
- Every request at every layer carries a verified Choiz email, so MCPs can audit per-user (see `x-mcp-user` header on the upstream side).

## Infrastructure

| Component | Where | Why |
|---|---|---|
| Cloudflare Worker `choiz-mcp-worker` | Cloudflare edge | OAuth 2.0 + DCR, Google federation, domain restriction. Hostname `mcp.choiz.com.mx`. |
| Cloudflare Tunnel `choiz-mcp-gateway` | Runs as `systemd` service on the EC2 | Publishes `http://localhost:8080` as `https://tunnel.choiz.com.mx` (used by the Worker) without opening inbound ports. |
| EC2 `t4g.small` (Ubuntu 24.04 ARM) | Same VPC as the RDS warehouse | Hosts the gateway + MCP containers. Admin access via AWS SSM Session Manager — no SSH, no VPN. |
| Docker Compose stack | On the EC2 at `~/choiz-mcp-gateway` | Two services in production: `gateway` + one container per MCP (currently `warehouse_mcp`). |
| Cloudflare KV `OAUTH_KV` | Cloudflare account | Stores DCR client registrations + issued bearer tokens for the Worker. |
| Google OAuth Client `choiz-mcp-gateway` | Google Cloud project | Internal user type, restricted to the choiz.com.mx Workspace. |

## Routes

| Path | Handled by | Auth | Notes |
|---|---|---|---|
| `GET /` | Worker (default handler) | — | Info page |
| `GET /.well-known/oauth-authorization-server` | Worker (OAuth provider lib) | — | Metadata Claude.ai reads to auto-configure |
| `GET /authorize` | Worker | — | Starts the Google OAuth flow |
| `GET /callback` | Worker | — | Google returns the user here |
| `POST /token` | Worker (OAuth provider lib) | — | Claude.ai exchanges code for bearer token |
| `POST /register` | Worker (OAuth provider lib) | — | Dynamic Client Registration endpoint |
| `POST /mcp/<name>/` | Worker → tunnel → gateway → MCP | Bearer token (minted by Worker) | MCP Streamable HTTP traffic |
| `GET /healthz` | Gateway (on localhost only) | — | Liveness probe |

Every `/mcp/*` request, by the time it reaches the gateway, carries two headers set by the Worker:

- `X-Worker-Shared-Secret` — must match `WORKER_SHARED_SECRET` in the gateway `.env`.
- `X-Choiz-User-Email` — Google-verified email, enforced to end in `@choiz.com.mx`.

Requests missing either header are rejected with `401` by the gateway. Requests missing a valid bearer token are rejected with `401` by the Worker before ever reaching the tunnel.

## Repository layout

```
choiz-mcp-gateway/
├── compose.yml                 # Stack definition: gateway + MCP containers
├── .env.example                # Template for WORKER_SHARED_SECRET, WAREHOUSE_DATABASE_URL, ...
├── gateway/                    # Auth + routing reverse proxy (Node/Express)
│   ├── Dockerfile
│   └── src/
│       ├── index.ts            # Routes + proxy setup
│       └── auth.ts             # Worker shared-secret validation
├── mcp/                        # One subfolder per MCP server
│   └── warehouse/
│       └── Dockerfile          # postgres-mcp (stdio) + supergateway wrapper
├── worker/                     # Cloudflare Worker (OAuth + proxy)
│   ├── wrangler.jsonc
│   └── src/
│       ├── index.ts            # OAuthProvider wiring
│       ├── default-handler.ts  # /authorize, /callback (Google flow)
│       ├── api-handler.ts      # Proxies authenticated /mcp/* to the tunnel
│       └── types.ts
└── docs/
    ├── ADDING_AN_MCP.md        # Recipe for adding a new MCP server
    ├── OPERATIONS.md           # Runbook: deploy, logs, rotate secrets
    └── NEXT_STEPS.md           # Current state + backlog
```

## Quick links

- Adding a new MCP to the gateway: [docs/ADDING_AN_MCP.md](docs/ADDING_AN_MCP.md)
- Operations / runbook: [docs/OPERATIONS.md](docs/OPERATIONS.md)
- Backlog + what to do next: [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md)

## Users: adding the connector in Claude.ai

1. Settings → Connectors → Add custom connector.
2. URL: `https://mcp.choiz.com.mx/mcp/<name>/` (trailing slash matters).
3. Sign in with your `@choiz.com.mx` account. Other domains are rejected.
4. The connector shows as **Connected**.

## Security notes

- The tunnel hostname (`tunnel.choiz.com.mx`) is technically reachable from the public internet; the shared secret is what prevents any caller other than the Worker from talking to the gateway. Treat it as a critical credential.
- MCP containers are never exposed on a host port; they only accept connections over the internal Docker network.
- The Postgres role used by the warehouse MCP must be read-only. `postgres-mcp --access-mode=restricted` is belt-and-suspenders on top of that.
- Bearer tokens issued by the Worker are stored in KV and are opaque (no user info encoded); revoking a user is a matter of removing them from Google Workspace.
