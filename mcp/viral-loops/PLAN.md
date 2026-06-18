# Viral Loops MCP — funnel + hardening plan

Execute this in a fresh session. It turns the current read-only Viral Loops MCP
(per-participant lookups) into something that can actually answer the PM funnel
question **without flooding claude.ai's context**, plus applies agreed code fixes.

The funnel need (Sergio): build the referral program's **end-to-end funnel over
the last few months, in absolutes and rates**. The Viral-Loops-owned steps are:
referral link generated → referred lead loads email → referred user pays
(conversion attributed to a referrer). Top-of-funnel (active users, app access,
in-app referral-section visits) comes from Mixpanel + BI, NOT this server.

---

## 0. Current state (read before starting)

- **Repo:** `c:/Users/uriel/A/_dev/ctl/common/data/choiz-mcp-gateway` (its own git repo, default branch `master`).
- **Git:** Mixpanel work is committed on branch **`feat/mixpanel-mcp`** (`e0907f5`). The **Viral Loops work is UNCOMMITTED** in the working tree on that branch:
  - new: `mcp/viral-loops/{entrypoint.py, Dockerfile}` (+ this `PLAN.md`)
  - edited: `gateway/src/index.ts`, `compose.yml`, `compose.dev.yml`, `.env.example`, `.github/workflows/deploy-gateway.yml`
  - **First step (task 0): move VL changes onto their own branch off `master`.** See below.
- **The VL MCP already works** (verified live): FastMCP + requests, `stateless_http=True`, serves at `/`, 8 read-only tools, compact JSON. Local docker build + `initialize`/`tools/list`/`tools/call` all pass.
- **Mixpanel is done** — don't touch it.

## Verified API facts (do NOT re-derive)

- Base URL: `https://app.viral-loops.com/api/v3` (override env `VIRAL_LOOPS_BASE_URL`).
- Auth: single header `apiToken: <token>`. **Campaign-scoped — no campaignId on any call.** Token is in `.env` as `VIRAL_LOOPS_API_TOKEN` (gitignored, present locally; NOT deployed — EC2 `.env` needs it set over SSM).
- Verified GET paths (in `PATHS` in entrypoint.py): `/campaign/data`, `/campaign/stats` (lifetime `leadCount`/`referralCountTotal`/`conversionCountTotal`, **no date filter**), `/campaign/participant/{data,referrals,rank,order,referrer}`, `/campaign/participant/rewards/{given,pending}`.
- Participant identifier = `referralCode` and/or `email` (NOT `user_id`).
- **`POST /campaign/participant/query`** returns **rows, 50/page**: `{"data": [ {user, referrer, counters}, ... ]}`. **No server-side count.** A naive `{"filter":{"createdAt":{"$gte":"..."}}}` returns HTTP 400 — the real filter/pagination schema is UNKNOWN and is task 1.
- **`POST /campaign/participant/search`** exists; empty body → 400 (needs a body; schema unknown).
- claude.ai silently rejects MCP tool results larger than **~2–3 KB**.

---

## Task 0 — Git hygiene

```bash
cd .../choiz-mcp-gateway
# from feat/mixpanel-mcp with VL changes in the working tree:
git stash -u                      # stash VL edits + untracked mcp/viral-loops/
git checkout master
git checkout -b feat/viral-loops
git stash pop                     # VL changes now on feat/viral-loops
git status                        # confirm only VL files changed; mixpanel NOT present
```
(If `git stash pop` conflicts on the shared files because the mixpanel commit isn't in master, instead cherry-pick or just re-apply — the VL hunks in `gateway/src/index.ts`/`compose.yml`/`.env.example` are independent of the mixpanel hunks, so a clean 3-way usually applies. Verify `grep -ri mixpanel` shows nothing staged.)

## Task 1 — Discover the `/query` (and `/search`) filter + pagination schema  ← do FIRST; tasks 3 & 4 depend on it

Goal: find how to (a) filter by **date range**, (b) filter by **conversion status / has-referrer**, (c) **paginate** (skip/limit/page/count?), and (d) whether any response carries a **total count**.

Recipe (write curl output to a **repo-relative** file — native Windows python can't read `/tmp`):
```bash
VLT=$(grep '^VIRAL_LOOPS_API_TOKEN=' .env | cut -d= -f2-); BASE="https://app.viral-loops.com/api/v3"
# vary the body; inspect keys + whether 'data' length changes / a count appears
curl -s -X POST "$BASE/campaign/participant/query" -H "apiToken: $VLT" -H "content-type: application/json" -d '{"filter":{}}' -o q.json -w "%{http_code}\n"
```
Things to try in the body: `{"limit":N,"skip":M}`, `{"page":1,"count":N}`, `{"filter":{...}}` with date keys like `joinedAt`/`createdAt`/`registeredAt` and operators the API actually accepts (the `$gte` guess failed — try ISO strings, epoch ms, `{from,to}` shapes), and conversion fields (look at what a real row's `counters`/`referrer`/`converted` fields are named — dump ONE row's keys+values for a converted vs non-converted participant). Also probe `/search` with a minimal valid body. **Document the working schema at the top of entrypoint.py.** If no count and no efficient server-side aggregation exists, note it — it changes task 3's design (see risk below).

Sanity cross-check: whatever totals you compute over "all time" should reconcile with `/campaign/stats` (`leadCount`, `referralCountTotal`, `conversionCountTotal`).

## Task 2 — Code fixes (the 2 review findings + 2 hardening adds)

In `mcp/viral-loops/entrypoint.py`:

1. **Validate `status`** in `get_participant_rewards` (don't silently fall back to "given"):
   ```python
   s = status.strip().lower()
   if s not in ("given", "pending"):
       raise ValueError('status must be "given" or "pending"')
   key = "rewards_pending" if s == "pending" else "rewards_given"
   ```
2. **Bound `page`/`count`** in `get_participant_referrals` (clamp, don't raise):
   ```python
   _MAX_REFERRALS_COUNT = 25   # module constant; ~fits the 2-3 KB ceiling
   page = max(1, int(page)); count = min(max(1, int(count)), _MAX_REFERRALS_COUNT)
   ```
   Note the clamp in the docstring.
3. **429 retry/backoff** in `_get` (add `import time`): retry up to 3x on 429, honor `Retry-After` (clamp 0.5–5 s). VL caps 300/min.
4. **Clean empty-200**: an empty 2xx body (e.g. participant with no referrer) currently returns `{"raw":""}`; make it return `"null"`.

## Task 3 — `get_referral_funnel(start, end)` — server-side aggregation (Option A)

A new tool that paginates `/query` (or `/search`) **internally**, counts, and returns a **small** funnel object — NOT rows. Target output (~a few hundred bytes):
```json
{"window":{"start":"...","end":"..."},
 "referrers_with_link": N, "referred_leads": N, "conversions": N,
 "rates":{"lead_to_referrer":x,"referrer_to_referred":x,"referred_to_conversion":x}}
```
Map the three counts to the real `/query` row fields discovered in task 1 (referral generated = participant has a referralCode / is flagged as referrer; referred lead = participant has a `referrer`; conversion = referred participant with a converted timestamp/counter). Add a `group_by="month"` variant if cheap.

**Design risk (decide after task 1):** if `/query` only returns rows (50/page, no count, no aggregate), a full campaign pass is ~820 requests for ~41k leads (~3 min, rate-limit-bound). Mitigations, in order of preference:
- a date-bounded window keeps the row set small → fine to paginate;
- if VL exposes any count/aggregate or a stats-with-date endpoint (task 1), use it;
- otherwise cap the window and `log()` if truncated, and lean on Task 4 (export) for big pulls.
Keep all looping/counting server-side so only the small summary crosses the tool channel.

## Task 4 — `export_participants(...)` → download URL (Option B; your file-download ask)

Mirror the **DHL externalization pattern** already in this repo — the cleanest reference:
- `mcp/dhl/entrypoint.py`: `_store_label()` (in-memory TTL store keyed by `secrets.token_urlsafe`), `@mcp.custom_route("/download/{token}", methods=["GET"])`, and `LABEL_DOWNLOAD_BASE_URL`.
- `gateway/src/index.ts`: the `/dl/dhl` block (public capability URL, NOT under `/mcp` auth, verifies worker shared secret, strips internal headers, rewrites `^/` → `/download/`).

Build:
- A tool that pulls the full filtered participant set (paginate `/query`), writes a **CSV or JSON** to an in-memory TTL store, returns a short `{"download_url": "https://mcp.choiz.com.mx/dl/viral-loops/<token>", "rows": N, "expires_in_minutes": ...}`. The tool result is just the URL → **no context flood**.
- A `/download/{token}` custom route in the entrypoint + a new **`/dl/viral-loops`** proxy block in `gateway/src/index.ts` (copy the dhl one, swap the upstream env to `UPSTREAM_VIRAL_LOOPS`).
- Env: `VIRAL_LOOPS_DOWNLOAD_BASE_URL` (default `https://mcp.choiz.com.mx/dl/viral-loops`) + a TTL, added to `compose.yml` + `.env.example`.

**Consumption caveat to put in the tool docstring:** in claude.ai the analysis sandbox has **no outbound network**, so Claude can't fetch the URL itself — the user downloads it (or re-uploads it to Claude as an attachment to script over). In Claude Code the agent can `curl` it directly.

## Task 5 — Wire, validate, test

- If task 4 added the `/dl/viral-loops` route: update `gateway/src/index.ts` (done in task 4), and confirm `compose.yml` passes the new env to `viral_loops_mcp`.
- Validate:
  - `cd gateway && npx tsc --noEmit -p tsconfig.json` → exit 0.
  - `docker compose -f compose.yml -f compose.dev.yml config` (set the `:?`-required envs to dummy values inline) → exit 0.
  - `python -m py_compile mcp/viral-loops/entrypoint.py`.
- Live test (docker daemon must be up):
  ```bash
  docker build -t vl-test ./mcp/viral-loops
  docker rm -f vl >/dev/null 2>&1
  VLT=$(grep '^VIRAL_LOOPS_API_TOKEN=' .env | cut -d= -f2-)
  docker run -d --name vl -e VIRAL_LOOPS_API_TOKEN="$VLT" -p 8099:8080 vl-test
  # initialize + tools/list + tools/call for get_referral_funnel and export_participants
  ```
  Confirm: funnel returns small numbers that reconcile with `/campaign/stats`; export returns a URL; `GET /download/<token>` on :8099 serves the file; `status="bad"` errors; no-referrer → `null`. Then `docker rm -f vl`.

## Task 6 — Commit

One commit on `feat/viral-loops`: "Add read-only Viral Loops MCP (funnel + export)". Don't commit `.env`. Optionally delete this `PLAN.md` before committing (or keep it — your call).

---

## Gotchas (learned the hard way)

- **Native Windows python can't read `/tmp`** — curl (git bash) writes there fine, but `python.exe` resolves `/tmp` to `C:\tmp`. Use repo-relative temp files for any python read step.
- **FastMCP pretty-prints dict returns** → inflates payload vs the 2–3 KB ceiling. The MCP already returns **compact JSON strings** via `_get`; keep new tools doing the same (return `str`, `json.dumps(..., separators=(",",":"))`). Annotate tools `-> str` (also dodges FastMCP outputSchema rejection).
- **`streamable_http_path="/"` + `stateless_http=True` + `host="0.0.0.0"`** are required (gateway strips `/mcp/viral-loops` and forwards to `/`).
- **6 wiring touchpoints for a container MCP** (already done for the base; only revisit if task 4 adds the `/dl` route or new env): `gateway/src/index.ts`, `compose.yml` (env + depends_on + service), `compose.dev.yml`, `.env.example`, `.github/workflows/deploy-gateway.yml` (changes filter + `build-viral-loops` job + deploy `needs`/`if`).
- **Deploy:** EC2 `.env` needs `VIRAL_LOOPS_API_TOKEN` over SSM (+ any new export env); gateway image must redeploy if `gateway/src/index.ts` changed (route table is compiled in).

## Explicitly out of scope

- **Option C (ETL → warehouse).** Dropped per decision. If ever revisited: a SEPARATE loader job lands VL data in the warehouse; analysts read via the EXISTING read-only `warehouse` MCP. No new MCP write access — this MCP stays read-only.
- Mixpanel (already committed).
