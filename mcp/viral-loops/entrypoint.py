"""Viral Loops MCP — READ-ONLY access to a Viral Loops referral campaign.

Wraps the Viral Loops Web API v3 (https://app.viral-loops.com/api/v3). Custom
FastMCP + requests server, no upstream package to wrap — the surface we use is a
handful of GET endpoints. Mirrors the dhl / tiktok-organic shape:
FastMCP + requests, stateless_http=True, no SDK.

READ-ONLY by design. Viral Loops' write surface (register participant, convert,
flag, edit, redeem rewards) is deliberately NOT exposed — none of the POST/PUT
endpoints are wired. The only way in is the gateway auth chain
(x-worker-shared-secret + Google Workspace email).

Auth model
----------
Viral Loops authenticates with a per-campaign token sent in the ``apiToken``
HTTP header. The token is CAMPAIGN-SCOPED: it identifies which campaign you are
querying, so there is no separate campaignId parameter on any call. Use the
secret ``apiToken`` (server-side), NOT the ``publicToken`` (client-safe, fewer
permissions). Get it from the Viral Loops dashboard → your campaign → Settings /
Installation → API. Static — no refresh.

Single tenant: one campaign = one VIRAL_LOOPS_API_TOKEN in env. If Choiz ever
runs separate Viral Loops campaigns per brand, follow the gateway multi-tenant
pattern (one image, N containers + N slugs, e.g. /mcp/viral-loops-choiz) — the
container code here is already brand-agnostic.

claude.ai payload ceiling
-------------------------
claude.ai silently rejects MCP tool results above ~2-3 KB (see
ADDING_AN_MCP.md). Tool returns here are serialized as COMPACT JSON (no indent)
and the list endpoint (referrals) defaults to a small page size; raise it
explicitly only when needed.

All v3 REST paths + the participant identifier (referralCode / email) were
verified against the live API on 2026-06-18 with a real campaign token. The
token alone scopes every call to its campaign — no campaignId is ever sent.

Bulk participant access — /campaign/participant/search (verified 2026-06-18)
---------------------------------------------------------------------------
The ONLY paginated bulk endpoint. POST a body of exactly::

    {"pagination": {"limit": <1..100>, "offset": <int>}}

and it returns a flat JSON LIST of participant rows (NOT wrapped in ``data``).
Hard facts established by probing the live API:
  * ``limit`` is capped at 100 (HTTP 400 above it). ``offset`` paginates.
  * There is NO server-side filter, sort, or total count. ``filter`` / ``sort``
    / ``orderBy`` / date keys in the body are SILENTLY IGNORED. Default order is
    by rank (referralCountTotal desc), stable enough for offset paging.
  * An offset PAST the end returns HTTP 503 (not an empty list), so a scan must
    stop the moment a page returns fewer than ``limit`` rows.
Each row is flat, e.g.::

    {"id":52117063, "email":..., "referralCode":..., "createdAt":"2023-10-18T15:12:29.000Z",
     "referrerId":0, "referrerEmail":null, "referrerReferralCode":null,
     "referralCountTotal":86, "conversionCountTotal":25, "converted":0, "rank":1, ...}

Funnel field mapping (RECONCILED against /campaign/stats over the full ~41k set
on 2026-06-18 — leadCount 40956 / referralCountTotal 4606 / conversionCountTotal
1540):
  * lead / "referral link generated" = any participant (each gets a referralCode
    + uniqueLink on join). Count of all rows ≈ leadCount.
  * referrer who actually referred  = referralCountTotal > 0   (2225 observed).
  * referred lead "loads email"     = referrerId set (>0) / referrerReferralCode
    not null  (4605 observed ≈ referralCountTotal 4606).
  * conversion "referred user pays" = converted == 1 (1539 observed ≈
    conversionCountTotal 1540); every converted participant is also a referred
    one, so conversions are a strict subset of referred leads.
``createdAt`` is the participant's JOIN time. The API exposes NO per-referral or
per-conversion timestamp and NO server-side date filter, so any date window is
applied CLIENT-SIDE by join date over a FULL campaign scan (see _scan_participants).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import random
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("viral_loops_mcp")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# --- Config from env ------------------------------------------------------

API_TOKEN = os.environ["VIRAL_LOOPS_API_TOKEN"]
BASE_URL = (
    os.environ.get("VIRAL_LOOPS_BASE_URL") or "https://app.viral-loops.com/api/v3"
).rstrip("/")

# Single source of truth for the v3 REST paths. All verified against the live
# API on 2026-06-18 with a real campaign token.
PATHS = {
    "campaign":          "/campaign/data",                  # public campaign info/config
    "stats":             "/campaign/stats",                 # leads + total referrals + conversions
    "participant":       "/campaign/participant/data",      # one participant's data
    "referrals":         "/campaign/participant/referrals", # who a participant referred
    "rank":              "/campaign/participant/rank",       # leaderboard / waitlist rank
    "order":             "/campaign/participant/order",      # join order position
    "referrer":          "/campaign/participant/referrer",   # who referred this participant
    "rewards_given":     "/campaign/participant/rewards/given",
    "rewards_pending":   "/campaign/participant/rewards/pending",
}

# Page-size ceiling for get_participant_referrals. ~25 referral rows is about
# as much as fits under claude.ai's ~2-3 KB tool-result ceiling.
_MAX_REFERRALS_COUNT = 25

# Rate-limit handling. Viral Loops caps requests at 300/min; on a 429 it may
# send a Retry-After header. Retry a few times, honoring Retry-After clamped to
# a sane band so a hostile/garbage value can't stall the tool call.
_RATE_LIMIT_RETRIES = 3
_RETRY_AFTER_MIN_S = 0.5
_RETRY_AFTER_MAX_S = 5.0

# Bulk-scan retry policy (used by _post, i.e. the /search pager). The scan runs
# several requests concurrently, so it brushes Viral Loops' burst limit more
# than interactive single GETs do — Cloudflare answers a burst with a 429
# "Too Many Requests" HTML page. Be PATIENT here: more attempts, exponential
# backoff up to 30 s (honoring Retry-After), plus jitter so concurrent workers
# don't resynchronize into another simultaneous burst (thundering herd).
_SCAN_MAX_RETRIES = 6
_SCAN_BACKOFF_CAP_S = 30.0

_SESSION = requests.Session()
_SESSION.headers.update({"apiToken": API_TOKEN, "Accept": "application/json"})


# --- HTTP helpers ---------------------------------------------------------


def _retry_after_seconds(resp: requests.Response, attempt: int) -> float:
    """Seconds to wait before a 429 retry. Honor Retry-After if present
    (clamped to [_RETRY_AFTER_MIN_S, _RETRY_AFTER_MAX_S]); otherwise back off
    linearly by attempt within the same band."""
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            wait = _RETRY_AFTER_MIN_S * (attempt + 1)
    else:
        wait = _RETRY_AFTER_MIN_S * (attempt + 1)
    return max(_RETRY_AFTER_MIN_S, min(wait, _RETRY_AFTER_MAX_S))


def _parse_body(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        # An empty 2xx body (e.g. a participant with no referrer) decodes to
        # nothing; represent it as JSON null rather than {"raw": ""}.
        if not resp.text.strip():
            return None
        return {"raw": resp.text}


def _raise_for_error(resp: requests.Response, method: str, path: str, data: Any) -> None:
    msg = None
    if isinstance(data, dict):
        msg = data.get("message") or data.get("error") or data.get("detail") or data.get("description")
    raise RuntimeError(
        f"Viral Loops API error on {method} {path} (HTTP {resp.status_code})"
        + (f": {msg}" if msg else f": {str(data)[:300]}")
    )


def _get(path: str, params: dict[str, Any] | None = None) -> str:
    """Authenticated GET against the Viral Loops v3 API.

    Returns the response as a COMPACT JSON string (no indentation): FastMCP
    pretty-prints dict returns, which inflates payloads against claude.ai's
    ~2-3 KB tool-result ceiling (see ADDING_AN_MCP.md). Retries on HTTP 429
    (honoring Retry-After). Raises RuntimeError on other non-2xx so the model
    gets actionable feedback rather than a silent empty result — Viral Loops
    error bodies usually carry a ``message`` / ``error``. An empty 2xx body is
    returned as the JSON literal ``"null"``.
    """
    url = f"{BASE_URL}{path}"
    # Drop None/empty params so we never send e.g. ?email=
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        resp = _SESSION.get(url, params=clean, timeout=30)
        if resp.status_code == 429 and attempt < _RATE_LIMIT_RETRIES:
            time.sleep(_retry_after_seconds(resp, attempt))
            continue
        break

    data = _parse_body(resp)
    if 200 <= resp.status_code < 300:
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    _raise_for_error(resp, "GET", path, data)


def _scan_backoff_seconds(resp: requests.Response, attempt: int) -> float:
    """Patient backoff for the bulk scan: honor Retry-After (clamped to
    [1, _SCAN_BACKOFF_CAP_S]); otherwise exponential 1,2,4,... capped, plus up
    to 1 s of jitter to de-sync concurrent workers."""
    raw = resp.headers.get("Retry-After")
    wait = None
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            wait = None
    if wait is None:
        wait = min(2.0 ** attempt, _SCAN_BACKOFF_CAP_S)
    wait = max(1.0, min(wait, _SCAN_BACKOFF_CAP_S))
    return wait + random.random()


def _post(path: str, body: dict[str, Any]) -> Any:
    """Authenticated POST against the Viral Loops v3 API, returning the PARSED
    body (dict/list/None — not a JSON string, since callers aggregate it).

    Retries patiently on 429 (Cloudflare burst limit) and 503 — Viral Loops
    returns 503 for an offset past the end of the set and occasionally under
    load (the bulk scanners avoid the past-the-end case by stopping on a short
    page). See _scan_backoff_seconds for the policy. Raises RuntimeError on
    other non-2xx.
    """
    url = f"{BASE_URL}{path}"
    resp = None
    for attempt in range(_SCAN_MAX_RETRIES + 1):
        resp = _SESSION.post(url, json=body, timeout=60)
        if resp.status_code in (429, 503) and attempt < _SCAN_MAX_RETRIES:
            time.sleep(_scan_backoff_seconds(resp, attempt))
            continue
        break

    data = _parse_body(resp)
    if 200 <= resp.status_code < 300:
        return data
    _raise_for_error(resp, "POST", path, data)


# --- Bulk participant scan (internal, for funnel + export) ----------------
#
# /campaign/participant/search is the only paginated bulk endpoint:
#   POST {"pagination":{"limit":<=100,"offset":N}} -> a flat JSON LIST of rows.
# It has NO server-side filter, sort or total-count (verified live 2026-06-18 —
# unknown filter/sort keys are silently ignored), so date/conversion filtering
# happens client-side here and the only way to count a window is a full scan.
# The default order is by rank (referralCountTotal desc), stable enough for
# offset paging. An offset past the end returns HTTP 503 (NOT an empty list),
# so we stop as soon as a page comes back shorter than the limit.
SEARCH_PATH = "/campaign/participant/search"
_SEARCH_PAGE_LIMIT = 100  # API hard cap; larger -> HTTP 400.
# Safety backstop so a runaway never loops forever. 600 pages * 100 = 60k rows,
# comfortably above the ~41k campaign size; if we ever hit it we log + stop.
_MAX_SCAN_PAGES = 600
# Concurrency for the bulk scan. Viral Loops documents 300/min, but Cloudflare
# fronts it with a tighter BURST limit that is sensitive to CONCURRENCY, not
# average rate (measured 2026-06-18: steady ~1 req/s serial never 429s; 8
# concurrent workers reliably tripped a 429 "Too Many Requests" storm). So keep
# concurrency low — 3 workers (~3 req/s) is well under 300/min and a gentle
# burst; the patient exponential backoff in _post (_scan_backoff_seconds) is the
# safety net for the occasional 429. A full ~412-page scan lands around 3-4 min.
_SCAN_WORKERS = 3


def _search_page(limit: int, offset: int) -> list[dict[str, Any]]:
    rows = _post(SEARCH_PATH, {"pagination": {"limit": limit, "offset": offset}})
    if not isinstance(rows, list):
        raise RuntimeError(
            f"Unexpected /search response (expected a list): {str(rows)[:200]}"
        )
    return rows


def _iter_participants(page_limit: int = _SEARCH_PAGE_LIMIT):
    """Yield every participant row, one page at a time, oldest scan order.

    Pages through /search until a short page (the last one). Caps at
    _MAX_SCAN_PAGES as a runaway guard. Dedupes by participant id so a row
    that shifts across a page boundary mid-scan is counted once.
    """
    limit = max(1, min(int(page_limit), _SEARCH_PAGE_LIMIT))
    seen: set[Any] = set()
    offset = 0
    for _ in range(_MAX_SCAN_PAGES):
        rows = _search_page(limit, offset)
        for r in rows:
            rid = r.get("id")
            if rid is not None and rid in seen:
                continue
            if rid is not None:
                seen.add(rid)
            yield r
        if len(rows) < limit:
            return
        offset += limit
    logger.warning("participant scan hit _MAX_SCAN_PAGES=%d cap; results truncated", _MAX_SCAN_PAGES)


def _campaign_lead_count() -> int | None:
    """Total participant count from /campaign/stats (leadCount), or None.

    Used purely to size the bulk scan up front so its pages can be fetched
    concurrently. None -> fall back to a serial scan.
    """
    try:
        data = json.loads(_get(PATHS["stats"]))
        n = data.get("leadCount") if isinstance(data, dict) else None
        return int(n) if n is not None else None
    except Exception:  # never let a stats hiccup block the scan
        return None


def _fetch_page_or_empty(offset: int) -> list[dict[str, Any]]:
    """A /search page, but a beyond-the-end offset (which Viral Loops answers
    with HTTP 503) yields [] instead of raising — so buffer pages past the real
    end of the set don't abort a concurrent scan. Real errors still raise."""
    try:
        return _search_page(_SEARCH_PAGE_LIMIT, offset)
    except RuntimeError as e:
        if " 503" in str(e):
            return []
        raise


def _scan_participants(max_pages: int | None = None) -> tuple[list[dict[str, Any]], int, bool]:
    """Fetch the full participant set. Returns (rows, pages_fetched, truncated).

    When the total is known (leadCount from /campaign/stats) the pages are
    fetched concurrently (_SCAN_WORKERS at a time); otherwise it falls back to a
    serial offset walk. Rows are deduped by id. `max_pages` caps the scan for a
    quick partial estimate — when the cap bites, `truncated` is True.
    """
    limit = _SEARCH_PAGE_LIMIT
    lead = _campaign_lead_count()
    rows_by_id: dict[Any, dict[str, Any]] = {}

    if lead and lead > 0:
        # +3 buffer pages cover rows added between the stats read and the scan;
        # the surplus pages simply 503 -> [] via _fetch_page_or_empty.
        needed = lead // limit + 3
        n_pages = min(needed, _MAX_SCAN_PAGES)
        truncated = needed > _MAX_SCAN_PAGES
        if max_pages is not None and max_pages > 0 and max_pages < n_pages:
            n_pages = max_pages
            truncated = True
        offsets = [i * limit for i in range(n_pages)]
        with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as ex:
            for page in ex.map(_fetch_page_or_empty, offsets):
                for r in page:
                    rid = r.get("id")
                    rows_by_id[rid if rid is not None else len(rows_by_id)] = r
        return list(rows_by_id.values()), n_pages, truncated

    # Fallback: total unknown -> serial walk, honoring max_pages.
    pages = 0
    truncated = False
    offset = 0
    while True:
        if max_pages is not None and max_pages > 0 and pages >= max_pages:
            truncated = True
            break
        rows = _search_page(limit, offset)
        pages += 1
        for r in rows:
            rid = r.get("id")
            rows_by_id[rid if rid is not None else len(rows_by_id)] = r
        if len(rows) < limit:
            break
        offset += limit
        if pages >= _MAX_SCAN_PAGES:
            truncated = True
            break
    return list(rows_by_id.values()), pages, truncated


# --- Date-window helpers (client-side; the API has no date filter) --------


def _norm_ts(s: str) -> str:
    """Normalize a date/ISO string to a fixed-width 'YYYY-MM-DDTHH:MM:SS' form
    for lexicographic comparison. Date-only input is padded to start-of-day.
    Viral Loops createdAt is UTC ('...Z'), and so is every window bound, so a
    plain string compare on this form is a correct chronological compare."""
    s = (s or "").strip().replace(" ", "T")
    if not s:
        return ""
    if len(s) == 10:  # YYYY-MM-DD
        return s + "T00:00:00"
    return s[:19]


def _in_window(row_ts: str, start_b: str, end_b: str) -> bool:
    """start inclusive, end exclusive. Empty start/end => unbounded that side."""
    ts = _norm_ts(row_ts)
    if start_b and ts < start_b:
        return False
    if end_b and ts >= end_b:
        return False
    return True


def _participant_params(referral_code: str, email: str) -> dict[str, Any]:
    """Build the participant-identifier query. Viral Loops identifies a
    participant by `referralCode` and/or `email` — at least one is required
    (the API rejects the call with "must contain at least one of
    [email, referralCode]" otherwise). Both are passed when given."""
    if not referral_code and not email:
        raise ValueError(
            "Provide at least one of referral_code or email to identify the participant."
        )
    return {"referralCode": referral_code, "email": email}


# --- Export file store (TTL, in-memory) -----------------------------------
#
# Mirrors the DHL label store: export_participants writes a CSV/JSON to this
# in-process store under an unguessable token and returns a short download_url
# instead of inlining tens of thousands of rows (which would overrun claude.ai's
# tool-result ceiling). The gateway proxies /dl/viral-loops/<token> -> this
# container's GET /download/<token>; the ~192-bit token is the capability.
# In-memory only: a container restart drops all links (exports are ephemeral —
# regenerate). Accessed from sync tool calls (anyio worker thread) and the async
# /download route, so guard with a plain Lock.
VIRAL_LOOPS_DOWNLOAD_BASE_URL = (
    os.environ.get("VIRAL_LOOPS_DOWNLOAD_BASE_URL")
    or "https://mcp.choiz.com.mx/dl/viral-loops"
).rstrip("/")
try:
    EXPORT_TTL_MINUTES = int(os.environ.get("VIRAL_LOOPS_EXPORT_TTL_MINUTES") or "30")
except ValueError:
    EXPORT_TTL_MINUTES = 30
# Cap stored exports so a burst can't grow the container unbounded. A full ~41k
# CSV is a few MB; 10 * a few MB stays well under the 256m container cap.
_MAX_EXPORTS = 10

_export_lock = threading.Lock()
_export_store: dict[str, dict[str, Any]] = {}


def _store_export(data: bytes, filename: str, content_type: str) -> str:
    """Stash an export under a fresh unguessable token. Returns the token."""
    token = secrets.token_urlsafe(24)  # ~192 bits
    now = time.time()
    with _export_lock:
        for t in [t for t, e in _export_store.items() if e["expires_at"] <= now]:
            _export_store.pop(t, None)
        if len(_export_store) >= _MAX_EXPORTS:
            for t in sorted(_export_store, key=lambda t: _export_store[t]["expires_at"])[
                : len(_export_store) - _MAX_EXPORTS + 1
            ]:
                _export_store.pop(t, None)
        _export_store[token] = {
            "data": data,
            "filename": filename,
            "content_type": content_type,
            "expires_at": now + EXPORT_TTL_MINUTES * 60,
        }
    return token


def _get_export(token: str) -> dict[str, Any] | None:
    """Return a non-expired stored export, or None (dropping it if expired)."""
    now = time.time()
    with _export_lock:
        entry = _export_store.get(token)
        if entry is None:
            return None
        if entry["expires_at"] <= now:
            _export_store.pop(token, None)
            return None
        return entry


# Columns emitted for a CSV export — a useful analyst subset of the flat
# /search row (the JSON export keeps every field).
_EXPORT_COLUMNS = [
    "id", "email", "firstname", "lastname", "referralCode", "createdAt",
    "referrerId", "referrerEmail", "referrerReferralCode",
    "referralCountTotal", "conversionCountTotal", "converted",
    "acquiredFrom", "fraudLevel", "risk", "rank",
]


# --- MCP server -----------------------------------------------------------

# host="0.0.0.0" so the gateway reaches us across the docker bridge.
# streamable_http_path="/" so the gateway can strip /mcp/viral-loops and forward to "/".
# stateless_http=True: ephemeral sessions, sidesteps stale-session-after-redeploy.
mcp = FastMCP(
    name="viral-loops",
    host="0.0.0.0",
    port=8080,
    streamable_http_path="/",
    stateless_http=True,
)


@mcp.custom_route("/download/{token}", methods=["GET"])
async def download_export(request: Request) -> Response:
    """Serve a stored export (CSV/JSON) by token as a browser download.

    Reached publicly (no MCP bearer) via the gateway + Worker:
    https://mcp.choiz.com.mx/dl/viral-loops/<token> -> gateway /dl/viral-loops
    -> here. The token is the capability; unknown/expired -> 404.
    """
    entry = _get_export(request.path_params.get("token", ""))
    if entry is None:
        return Response("Export not found or expired.", status_code=404, media_type="text/plain")
    return Response(
        content=entry["data"],
        media_type=entry["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{entry["filename"]}"'},
    )


@mcp.tool()
def get_campaign() -> str:
    """Get the public configuration/info of the Viral Loops campaign.

    The campaign is determined by the server's API token — there is no campaign
    id to pass. Read-only. Calls GET /campaign/data.
    """
    return _get(PATHS["campaign"])


@mcp.tool()
def get_campaign_stats() -> str:
    """Get headline campaign stats: leadCount, referralCountTotal,
    conversionCountTotal.

    The single best "how is the referral program doing overall" call. Read-only.
    Calls GET /campaign/stats.
    """
    return _get(PATHS["stats"])


@mcp.tool()
def get_participant(referral_code: str = "", email: str = "") -> str:
    """Fetch one participant's data (referral code, rank, referral counts, state).

    Identify the participant by `referral_code` and/or `email` — at least one is
    required. Read-only. Calls GET /campaign/participant/data.
    """
    return _get(PATHS["participant"], _participant_params(referral_code, email))


@mcp.tool()
def get_participant_referrals(
    referral_code: str = "", email: str = "", page: int = 1, count: int = 10
) -> str:
    """List the people a given participant referred.

    Identify the referrer by `referral_code` and/or `email` (at least one
    required). `page` is 1-based; `count` is the page size — kept small
    (default 10) because claude.ai rejects large tool results. `count` is
    clamped to [1, 25] and `page` to >=1 (the ceiling keeps the result under
    claude.ai's ~2-3 KB tool-result limit). Read-only.
    Calls GET /campaign/participant/referrals.
    """
    params = _participant_params(referral_code, email)
    page = max(1, int(page))
    count = min(max(1, int(count)), _MAX_REFERRALS_COUNT)
    # This endpoint paginates by limit/offset; page/count are SILENTLY IGNORED
    # (verified live 2026-06-18 — count=5 still returned 50 rows, but limit=5
    # returned 5). Map the friendly page/count interface onto limit/offset so
    # the clamp actually bounds the payload and paging works.
    params.update({"limit": count, "offset": (page - 1) * count})
    return _get(PATHS["referrals"], params)


@mcp.tool()
def get_participant_rank(referral_code: str = "", email: str = "") -> str:
    """Get a participant's leaderboard / waitlist rank.

    Identify by `referral_code` and/or `email` (at least one required). Read-only.
    Calls GET /campaign/participant/rank.
    """
    return _get(PATHS["rank"], _participant_params(referral_code, email))


@mcp.tool()
def get_participant_order(referral_code: str = "", email: str = "") -> str:
    """Get the order/position in which a participant joined the campaign.

    Identify by `referral_code` and/or `email` (at least one required). Read-only.
    Calls GET /campaign/participant/order.
    """
    return _get(PATHS["order"], _participant_params(referral_code, email))


@mcp.tool()
def get_participant_referrer(referral_code: str = "", email: str = "") -> str:
    """Get the referrer of a participant (who invited them).

    Identify by `referral_code` and/or `email` (at least one required). Read-only.
    Calls GET /campaign/participant/referrer.
    """
    return _get(PATHS["referrer"], _participant_params(referral_code, email))


@mcp.tool()
def get_participant_rewards(
    referral_code: str = "", email: str = "", status: str = "given"
) -> str:
    """Get one participant's rewards. `status` is "given" (distributed) or
    "pending" (earned but not yet redeemed).

    Identify by `referral_code` and/or `email` (at least one required). An
    identifier is mandatory on purpose: without one the endpoint returns the
    WHOLE campaign's rewards (thousands of entries), which overruns claude.ai's
    tool-result limit. Read-only.
    Calls GET /campaign/participant/rewards/{given|pending}.
    """
    s = status.strip().lower()
    if s not in ("given", "pending"):
        raise ValueError('status must be "given" or "pending"')
    key = "rewards_pending" if s == "pending" else "rewards_given"
    return _get(PATHS[key], _participant_params(referral_code, email))


# --- Funnel + export (bulk aggregation over /search) ----------------------


def _is_referred(row: dict[str, Any]) -> bool:
    return bool(row.get("referrerId")) or bool(row.get("referrerReferralCode"))


def _is_converted(row: dict[str, Any]) -> bool:
    return row.get("converted") == 1 or row.get("converted") is True


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _tally(rows: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    """(leads, referrers_with_link, referred_leads, conversions) for `rows`."""
    leads = referrers = referred = conversions = 0
    for r in rows:
        leads += 1
        if (r.get("referralCountTotal") or 0) > 0:
            referrers += 1
        if _is_referred(r):
            referred += 1
            if _is_converted(r):
                conversions += 1
    return leads, referrers, referred, conversions


def _funnel_block(counts: tuple[int, int, int, int]) -> dict[str, Any]:
    leads, referrers, referred, conversions = counts
    return {
        "leads": leads,
        "referrers_with_link": referrers,
        "referred_leads": referred,
        "conversions": conversions,
        "rates": {
            "lead_to_referrer": _rate(referrers, leads),
            "referrer_to_referred": _rate(referred, referrers),
            "referred_to_conversion": _rate(conversions, referred),
        },
    }


@mcp.tool()
def get_referral_funnel(
    start: str = "", end: str = "", group_by: str = "", max_pages: int = 0
) -> str:
    """End-to-end referral funnel for a date window — small summary, NOT rows.

    Returns the Viral-Loops-owned funnel steps over participants who JOINED in
    the window [start, end):
      • leads               — all participants (each gets a referral link on join)
      • referrers_with_link — participants who actually referred ≥1 person
      • referred_leads      — participants who arrived via someone's referral link
      • conversions         — referred leads who paid (converted)
    plus rates: lead_to_referrer, referrer_to_referred (referred per referrer),
    referred_to_conversion. (Reconciled with /campaign/stats on 2026-06-18.)

    `start`/`end` are dates ("YYYY-MM-DD") or ISO timestamps, UTC; start is
    inclusive, end exclusive; leave either blank for unbounded that side.
    `group_by="month"` adds a per-join-month breakdown — keep the window narrow
    or that object can exceed claude.ai's result limit.

    HOW IT WORKS / COST: the Viral Loops API has NO server-side date filter, sort
    or count, so this does a FULL campaign scan every call (~41k participants,
    paged 100 at a time, fetched concurrently) — roughly 3-5 minutes. A date
    window does NOT make it cheaper. `max_pages` (0 = all) caps the scan for a
    quick partial estimate; the result then carries scan.truncated=true. Counts
    are computed server-side; only this small summary crosses the tool channel.
    Read-only.
    """
    start_b, end_b = _norm_ts(start), _norm_ts(end)
    rows, pages, truncated = _scan_participants(max_pages or None)
    windowed = [r for r in rows if _in_window(r.get("createdAt", ""), start_b, end_b)]

    out: dict[str, Any] = {"window": {"start": start or None, "end": end or None}}
    out.update(_funnel_block(_tally(windowed)))

    if group_by.strip().lower() in ("month", "months"):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in windowed:
            buckets.setdefault((r.get("createdAt") or "")[:7], []).append(r)
        months: dict[str, Any] = {}
        for m in sorted(buckets):
            if not m:
                continue
            leads, referrers, referred, conversions = _tally(buckets[m])
            months[m] = {
                "leads": leads,
                "referrers_with_link": referrers,
                "referred_leads": referred,
                "conversions": conversions,
                "referred_to_conversion": _rate(conversions, referred),
            }
        out["months"] = months

    out["scan"] = {
        "participants_scanned": len(rows),
        "pages": pages,
        "truncated": truncated,
    }
    return json.dumps(out, separators=(",", ":"), ensure_ascii=False)


@mcp.tool()
def export_participants(
    start: str = "",
    end: str = "",
    fmt: str = "csv",
    referred_only: bool = False,
    converted_only: bool = False,
    max_pages: int = 0,
) -> str:
    """Export the full filtered participant set to a downloadable file; returns a
    short link, NOT the rows (so it never floods the tool channel).

    Scans the whole campaign, filters by JOIN-date window [start, end) (dates or
    ISO, UTC; start inclusive, end exclusive; blank = unbounded), optionally to
    `referred_only` (arrived via a referral) or `converted_only` (referred AND
    paid), serializes to `fmt` ("csv" — an analyst column subset — or "json" —
    every field), and stores it under a one-time token. Returns:
      {"download_url","rows","format","expires_in_minutes","window","scan"}.

    CONSUMPTION CAVEAT: in claude.ai the analysis sandbox has NO outbound
    network, so Claude CANNOT fetch this URL itself — give it to the user to
    open in a browser (or have them re-upload the file to Claude as an
    attachment to analyze). In Claude Code the agent can curl it directly. The
    link expires after expires_in_minutes and dies on container restart.

    COST: same full-scan as get_referral_funnel (~3-5 min; the API offers no
    server-side filter/count). `max_pages` (0 = all) caps the scan; the result
    then carries scan.truncated=true. Read-only.
    """
    f = fmt.strip().lower()
    if f not in ("csv", "json"):
        raise ValueError('fmt must be "csv" or "json"')

    start_b, end_b = _norm_ts(start), _norm_ts(end)
    rows, pages, truncated = _scan_participants(max_pages or None)
    selected = []
    for r in rows:
        if not _in_window(r.get("createdAt", ""), start_b, end_b):
            continue
        if converted_only and not (_is_referred(r) and _is_converted(r)):
            continue
        if referred_only and not _is_referred(r):
            continue
        selected.append(r)

    if f == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in selected:
            writer.writerow({k: r.get(k) for k in _EXPORT_COLUMNS})
        # utf-8-sig: a BOM so Excel reads accented names as UTF-8, not mojibake.
        data = buf.getvalue().encode("utf-8-sig")
        content_type = "text/csv; charset=utf-8"
        ext = "csv"
    else:
        data = json.dumps(selected, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
        ext = "json"

    label = (start or "all").replace(":", "").replace(" ", "_")
    if end:
        label += "_to_" + end.replace(":", "").replace(" ", "_")
    filename = f"viral-loops-participants_{label}.{ext}"
    token = _store_export(data, filename, content_type)

    return json.dumps(
        {
            "download_url": f"{VIRAL_LOOPS_DOWNLOAD_BASE_URL}/{token}",
            "rows": len(selected),
            "format": f,
            "bytes": len(data),
            "expires_in_minutes": EXPORT_TTL_MINUTES,
            "window": {"start": start or None, "end": end or None},
            "scan": {"participants_scanned": len(rows), "pages": pages, "truncated": truncated},
            "note": "Give this link to the user to open in a browser — claude.ai's "
                    "sandbox cannot fetch it. In Claude Code you can curl it directly.",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def main() -> None:
    logger.info("viral-loops mcp starting — base=%s", BASE_URL)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
