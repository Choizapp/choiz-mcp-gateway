# Operations runbook

Day-2 operations for the MCP gateway. Written so someone who did not set it up can keep it running.

## Access to the EC2

All admin access is over AWS SSM Session Manager. No SSH, no bastion, no VPN.

- AWS console → EC2 → select the instance tagged `choiz-mcp-gateway` → **Connect** → Session Manager → Connect.
- From the AWS CLI: `aws ssm start-session --target <instance-id>`.

The SSM user is `ssm-user`. The repo lives at `~/choiz-mcp-gateway`.

## Daily / weekly checks

Nothing is strictly required, but if you want to peek:

```bash
# Stack health
cd ~/choiz-mcp-gateway && docker compose ps

# Recent logs for each service
docker compose logs gateway       --tail=50
docker compose logs warehouse_mcp --tail=50

# Cloudflare Tunnel
sudo systemctl status cloudflared --no-pager

# Worker health from outside
curl -i https://mcp.choiz.com.mx/
curl -i https://tunnel.choiz.com.mx/healthz
```

The Worker has observability enabled; logs also live in the Cloudflare dashboard under Workers → `choiz-mcp-worker` → Logs.

## Deploying a change

### Gateway or MCP containers

```bash
git push origin master
```

That's it. GitHub Actions builds the changed images on `linux/arm64`, pushes them to GHCR, and runs `docker compose pull && up -d` on the EC2 via SSM. Watch the run at https://github.com/Choizapp/choiz-mcp-gateway/actions. End-to-end takes ~5-15 min depending on which images rebuild (meta-ads is the slowest under QEMU).

See [CICD.md](CICD.md) for the full pipeline reference, secrets/role setup, and rollback procedure.

If CI/CD is unavailable and you need to deploy by hand from the EC2:

```bash
# Edit /home/ssm-user/choiz-mcp-gateway/compose.yml inline if needed
cd /home/ssm-user/choiz-mcp-gateway
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

This pulls the latest `:latest` image from GHCR. Works as long as `docker login ghcr.io` is still cached as root.

### Cloudflare Worker

CI/CD also handles this — push any change under `worker/**` to `master` and `.github/workflows/deploy-worker.yml` runs `wrangler deploy`. Manual fallback from your laptop:

```bash
cd worker
npx wrangler deploy
```

### Cloudflare Tunnel config

Edits the tunnel's ingress rules. The service reads `/etc/cloudflared/config.yml`, not the user home version, so:

```bash
sudo nano /etc/cloudflared/config.yml
cloudflared tunnel ingress validate
sudo systemctl restart cloudflared
sudo systemctl status cloudflared --no-pager
```

## Tailing Worker logs in real time

```bash
cd worker
npx wrangler tail --format pretty
```

Great for debugging `/authorize` / `/token` issues from the outside.

## Rotating secrets

### WORKER_SHARED_SECRET

This secret must match on both sides: the `.env` on the EC2 AND the Worker's secret.

1. Generate a new value: `openssl rand -hex 32`.
2. On the EC2:
   ```bash
   nano ~/choiz-mcp-gateway/.env           # update WORKER_SHARED_SECRET
   docker compose up -d                    # picks up the new env
   ```
3. From your laptop in `worker/`:
   ```bash
   npx wrangler secret put WORKER_SHARED_SECRET
   # paste the same value
   npx wrangler deploy
   ```
4. There will be a window of seconds where one side has the new value and the other doesn't — requests in that window will 401. Plan accordingly.

### GOOGLE_CLIENT_SECRET

Rotated in Google Cloud Console (Credentials → your OAuth client → Rotate). Then:

```bash
cd worker
npx wrangler secret put GOOGLE_CLIENT_SECRET
npx wrangler deploy
```

Connected users keep working until their next `/authorize` round-trip (they hold a bearer token issued by us, not Google).

### WAREHOUSE_DATABASE_URL

Same pattern — edit `.env` on the EC2, `docker compose up -d`. Only `warehouse_mcp` needs restarting.

## Revoking a user

Remove them from Google Workspace (or from the `choiz.com.mx` group that can sign in). The next time they try to auth, Google rejects them; the Worker never mints a token for a non-`@choiz.com.mx` email. Their existing bearer token keeps working until it expires or is manually purged from KV.

To nuke a specific active token immediately: identify it in the Cloudflare KV dashboard (`OAUTH_KV` namespace, key prefix `token:` or similar depending on the OAuth provider library's schema) and delete it.

## Troubleshooting playbook

| Symptom | Likely cause | Where to look |
|---|---|---|
| Claude.ai says "Authorization failed" right after Google login | Redirect URI in Google does not exactly match `https://mcp.choiz.com.mx/callback`, OR user is not in `@choiz.com.mx`, OR OAuth consent screen is `External` + user not a test user | `wrangler tail` shows the `/callback` status; Google Cloud Console shows audience type |
| Claude.ai connects but every tool call fails with 401 | `WORKER_SHARED_SECRET` drift between Worker and EC2 `.env` | Compare `wrangler secret list` with `grep ^WORKER_SHARED_SECRET ~/choiz-mcp-gateway/.env` |
| Claude.ai connects but tool call returns 502 | MCP container crashed or is unreachable from the gateway | `docker compose ps`, `docker compose logs <name>_mcp --tail=100` |
| `https://mcp.choiz.com.mx/` returns Cloudflare 1033 or similar | Tunnel is down | `sudo systemctl status cloudflared` |
| MCP container keeps restarting | Usually bad credentials in env, or missing `--streamableHttpPath /` in supergateway command | `docker compose logs <name>_mcp` |
| "upstream_unavailable" from gateway | MCP container is up but not listening on 8080, or gateway's `UPSTREAM_*` env var is wrong | Exec into gateway container and `curl http://<name>_mcp:8080` |

## Backups

There is intentionally no state worth backing up on the EC2.

- The repo is the source of truth; it lives in GitHub.
- Secrets live on the EC2 `.env` + in Wrangler secrets. Store the master copies in a password manager.
- Cloudflare KV (tokens + DCR registrations) rebuilds itself: users re-auth after a wipe and connectors re-register.
- If the EC2 is destroyed, the recovery path is: launch a new EC2 (ARM64 / Graviton) with the same IAM role + SSM agent, copy `compose.yml` from the repo and restore `.env`, `sudo docker login ghcr.io` as root, `sudo docker compose pull && up -d`, then re-run the cloudflared setup (login + create + config + systemd install). Update `EC2_INSTANCE_ID` in GitHub repo secrets so CI/CD targets the new instance.

## Cost

At the time of writing, the stack runs entirely on free or near-free tiers:

- EC2 t4g.small: ~USD 13/mo.
- Cloudflare Workers: free tier (100k requests/day).
- Cloudflare Tunnel: free.
- Cloudflare KV: free tier (1000 writes/day).
- Google OAuth: free.
- RDS: already paid for by the data team.

Total marginal cost of this project: the EC2 bill.
