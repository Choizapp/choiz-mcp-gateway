"""TikTok Organic MCP — read-only access to ONE TikTok account's owned content.

Single-tenant by design (since 2026-05-19, mirrors the powerbi split): the
refresh token and brand label are pre-configured per-container via env vars.
A second tenant (e.g. timeless) gets its own container under the same image,
exposed at a different slug (/mcp/tiktok-organic-timeless/). This matches the
ga4-choiz / ga4-timeless / powerbi-choiz / powerbi-timeless convention and
keeps the LLM from picking the wrong brand at tool-call time.

End users authenticate to claude.ai with Google Workspace. The gateway routes
their request here; this container talks to TikTok Display API v2 using a
long-lived refresh_token kept in env. End users never see a TikTok login.

Tool surface (read-only):
  - get_account_info()                 : profile + counts (followers, videos, likes)
  - get_latest_videos(max_count=10)    : recent posts + engagement metrics
  - get_video_details(video_id)        : detail + engagement for one video
  - validate_token()                   : ping the API; refresh in-memory if needed

Auth model:
  TikTok Display API v2 OAuth.
    * access_token  : 24-hour TTL. Refreshed in-process on-demand.
    * refresh_token : ~365-day TTL, does NOT rotate on refresh in our
      experience (see project_tiktok_organic_deployed_state). Static env
      var; rotate manually before expiry by re-authorizing in the TikTok
      Developer Portal.
  The container NEVER writes tokens to disk. The local server.py used a
  tokens.json file; that pattern is incompatible with an ephemeral
  container and is dropped here. A new refresh_token returned by TikTok
  (should it ever rotate) is logged + discarded; if refresh starts to
  fail because of rotation, we re-mint manually.

Payload-ceiling discipline (see project_claudeai_payload_ceiling):
  list responses default to small page sizes and trim heavy fields
  (cover_image_url, full descriptions). get_video_details returns
  everything for one video, which stays comfortably under the ceiling.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("tiktok_organic_mcp")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# --- Config from env (all required except BRAND) --------------------------

CLIENT_KEY = os.environ["TIKTOK_CLIENT_KEY"]
CLIENT_SECRET = os.environ["TIKTOK_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["TIKTOK_REFRESH_TOKEN"]
# Short label baked into the MCP `name` so claude.ai surfaces it as
# "tiktok-organic-choiz" instead of a generic "tiktok-organic".
BRAND = os.environ.get("TIKTOK_BRAND", "unknown")

BASE_URL = "https://open.tiktokapis.com"

# --- Token cache (in-process, thread-safe) --------------------------------
#
# TikTok access_tokens last 24h. We mint on first use and silently refresh
# ~5 minutes before expiry on subsequent calls. No persistence — a redeploy
# just re-mints from the static refresh_token in env.

_token_lock = threading.Lock()
_access_token: str | None = None
_token_expires_at: float = 0.0  # epoch seconds


def _refresh_access_token() -> str:
    """Exchange the static refresh_token for a fresh access_token.

    Mutates module globals under `_token_lock`. Returns the new access_token.
    Raises RuntimeError if TikTok refuses the refresh — at that point the
    refresh_token has likely expired (>365d since last re-auth) and
    requires manual rotation in the Developer Portal.
    """
    global _access_token, _token_expires_at

    resp = requests.post(
        f"{BASE_URL}/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": CLIENT_KEY,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
        },
        timeout=20,
    )
    data = resp.json()

    if resp.status_code != 200 or "access_token" not in data:
        raise RuntimeError(
            f"refresh_access_token failed (HTTP {resp.status_code}): "
            f"error={data.get('error')} description={data.get('error_description')}. "
            "If this persists, the refresh_token has expired — re-authorize "
            "in the TikTok Developer Portal and update TIKTOK_*_REFRESH_TOKEN."
        )

    new_access = data["access_token"]
    expires_in = int(data.get("expires_in", 86400))
    # Subtract a safety margin so we never serve a token within 5 min of expiry.
    _access_token = new_access
    _token_expires_at = time.time() + expires_in - 300

    returned_refresh = data.get("refresh_token")
    if returned_refresh and returned_refresh != REFRESH_TOKEN:
        logger.warning(
            "TikTok returned a rotated refresh_token; container env still holds "
            "the previous one. If next refresh fails, update TIKTOK_*_REFRESH_TOKEN "
            "in EC2 .env from the Developer Portal."
        )
    return new_access


def _get_token() -> str:
    """Return a non-expired access_token, refreshing under the lock if needed."""
    global _access_token, _token_expires_at
    with _token_lock:
        if _access_token is None or time.time() >= _token_expires_at:
            return _refresh_access_token()
        return _access_token


# --- TikTok API helpers ---------------------------------------------------

# Error codes that mean "access_token bad", warranting one retry after refresh.
RETRYABLE_ERROR_CODES = {
    "access_token_invalid",
    "token_expired",
    "invalid_token",
}


def _api_request(
    method: str,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Make an authenticated request to TikTok Display API v2.

    Retries once if the access_token is rejected — covers the edge case where
    the cached token gets invalidated server-side before its expires_at.

    Raises RuntimeError on non-2xx HTTP or on a non-retryable TikTok error,
    so the LLM gets actionable feedback. Tools that want to surface the
    error as a dict (not raise) can catch RuntimeError.
    """
    url = f"{BASE_URL}{endpoint}"
    if fields:
        params = dict(params or {})
        params["fields"] = ",".join(fields)

    for attempt in (1, 2):
        token = _get_token()
        headers = {"Authorization": f"Bearer {token}"}
        if method == "POST":
            headers["Content-Type"] = "application/json"
            resp = requests.post(
                url, headers=headers, params=params, json=body or {}, timeout=30
            )
        else:
            resp = requests.get(url, headers=headers, params=params, timeout=30)

        data = resp.json()
        error = data.get("error", {}) or {}
        code = error.get("code")

        if code == "ok":
            return data

        if attempt == 1 and code in RETRYABLE_ERROR_CODES:
            # Force a re-mint on the next call.
            global _token_expires_at
            with _token_lock:
                _token_expires_at = 0.0
            continue

        raise RuntimeError(
            f"TikTok API error on {method} {endpoint}: code={code} "
            f"message={error.get('message')} log_id={error.get('log_id')}"
        )

    # Unreachable: the for-loop either returns or raises.
    raise RuntimeError("unreachable")


def _truncate(s: Any, n: int) -> Any:
    """Trim long strings to keep responses under claude.ai's payload ceiling."""
    if isinstance(s, str) and len(s) > n:
        return s[: n - 1] + "…"
    return s


# --- MCP server -----------------------------------------------------------

# host="0.0.0.0" so the gateway can reach us across the docker bridge with
#   Host: tiktok_organic_*_mcp:8080 (changeOrigin: true). FastMCP otherwise
#   auto-enables DNS rebinding protection that rejects non-localhost Host
#   headers — same gotcha as powerbi / warehouse.
# streamable_http_path="/" so the gateway can strip /mcp/tiktok-organic-<brand>
#   and forward to "/".
# stateless_http=True: every request is an ephemeral session. Sidesteps the
#   post-redeploy "stale Mcp-Session-Id" failure mode
#   (feedback_stale_session_after_redeploy).
mcp = FastMCP(
    name=f"tiktok-organic-{BRAND}",
    host="0.0.0.0",
    port=8080,
    streamable_http_path="/",
    stateless_http=True,
)


@mcp.tool()
def get_account_info() -> dict[str, Any]:
    """Get this brand's TikTok account profile + lifetime counts.

    Returns: display_name, bio_description, follower_count, following_count,
    likes_count (lifetime), video_count. The avatar_url is omitted (heavy,
    not useful for LLM reasoning).

    Note: the Display API does NOT expose date-ranged engagement metrics,
    only lifetime totals. For per-video engagement use `get_latest_videos`
    or `get_video_details`.
    """
    data = _api_request(
        "GET",
        "/v2/user/info/",
        fields=[
            "display_name",
            "bio_description",
            "follower_count",
            "following_count",
            "likes_count",
            "video_count",
        ],
    )
    user = data.get("data", {}).get("user", {})
    return {"brand": BRAND, **user}


@mcp.tool()
def get_latest_videos(
    max_count: int = 10,
    include_cover_url: bool = False,
) -> dict[str, Any]:
    """Get this brand's most recent TikTok videos with engagement metrics.

    Paginates /v2/video/list/ in pages of 20 (TikTok's max page size) until
    `max_count` is reached, then fetches engagement metrics in batches of 20
    via /v2/video/query/.

    Trimmed by default to stay under claude.ai's payload ceiling:
      * default max_count=10 (vs 20 in the legacy local server)
      * descriptions truncated to ~200 chars
      * cover_image_url omitted unless `include_cover_url=True`

    Each video carries: id, title, video_description (trimmed), duration,
    create_time (epoch seconds), view_count, like_count, comment_count,
    share_count. Engagement values are lifetime totals — the Display API
    does not support date-range filtering.
    """
    max_count = max(1, min(int(max_count), 100))

    # Step 1 — paginate /video/list/.
    all_videos: list[dict[str, Any]] = []
    cursor = 0
    while len(all_videos) < max_count:
        page_size = min(20, max_count - len(all_videos))
        list_result = _api_request(
            "POST",
            "/v2/video/list/",
            fields=[
                "id",
                "title",
                "video_description",
                "duration",
                "cover_image_url",
                "create_time",
            ],
            body={"max_count": page_size, "cursor": cursor},
        )
        page_videos = list_result.get("data", {}).get("videos", [])
        if not page_videos:
            break
        all_videos.extend(page_videos)
        has_more = list_result.get("data", {}).get("has_more", False)
        if not has_more:
            break
        cursor = list_result["data"]["cursor"]

    if not all_videos:
        return {"brand": BRAND, "videos": [], "count": 0}

    # Step 2 — fetch engagement metrics in batches of 20.
    metrics_by_id: dict[str, dict[str, Any]] = {}
    for i in range(0, len(all_videos), 20):
        batch_ids = [v["id"] for v in all_videos[i : i + 20]]
        query_result = _api_request(
            "POST",
            "/v2/video/query/",
            fields=["id", "view_count", "like_count", "comment_count", "share_count"],
            body={"filters": {"video_ids": batch_ids}},
        )
        for v in query_result.get("data", {}).get("videos", []):
            metrics_by_id[v["id"]] = v

    # Step 3 — merge + trim heavy fields.
    merged: list[dict[str, Any]] = []
    for video in all_videos:
        entry: dict[str, Any] = {
            "id": video.get("id"),
            "title": _truncate(video.get("title"), 100),
            "video_description": _truncate(video.get("video_description"), 200),
            "duration": video.get("duration"),
            "create_time": video.get("create_time"),
        }
        if include_cover_url:
            entry["cover_image_url"] = video.get("cover_image_url")
        metrics = metrics_by_id.get(video["id"], {})
        entry["view_count"] = metrics.get("view_count", 0)
        entry["like_count"] = metrics.get("like_count", 0)
        entry["comment_count"] = metrics.get("comment_count", 0)
        entry["share_count"] = metrics.get("share_count", 0)
        merged.append(entry)

    return {"brand": BRAND, "videos": merged, "count": len(merged)}


@mcp.tool()
def get_video_details(video_id: str) -> dict[str, Any]:
    """Get detailed information + engagement for a single TikTok video by ID.

    Single-video payload is small enough to return all available fields
    without trimming. Use this after `get_latest_videos` when you need
    extra detail on a specific post.

    Returns: id, title, video_description, duration, height, width,
    cover_image_url, share_url, embed_link, create_time, view_count,
    like_count, comment_count, share_count.
    """
    data = _api_request(
        "POST",
        "/v2/video/query/",
        fields=[
            "id",
            "title",
            "video_description",
            "duration",
            "height",
            "width",
            "cover_image_url",
            "share_url",
            "embed_link",
            "create_time",
            "view_count",
            "like_count",
            "comment_count",
            "share_count",
        ],
        body={"filters": {"video_ids": [video_id]}},
    )
    videos = data.get("data", {}).get("videos", [])
    if not videos:
        raise RuntimeError(f"video {video_id} not found")
    return {"brand": BRAND, **videos[0]}


@mcp.tool()
def validate_token() -> dict[str, Any]:
    """Ping TikTok with the current access_token + report cache state.

    Useful for debugging "is this MCP healthy" without exercising a real
    endpoint. Does NOT write to disk. If the cached token is expired,
    forces an in-memory refresh as a side effect.
    """
    # Force a refresh-if-needed pass + a lightweight call.
    data = _api_request("GET", "/v2/user/info/", fields=["open_id"])
    open_id = data.get("data", {}).get("user", {}).get("open_id")
    seconds_left = max(0, int(_token_expires_at - time.time()))
    return {
        "brand": BRAND,
        "valid": True,
        "open_id_present": bool(open_id),
        "access_token_seconds_remaining": seconds_left,
    }


def main() -> None:
    logger.info("tiktok-organic mcp starting — brand=%s", BRAND)
    try:
        _get_token()
        logger.info("initial token acquisition OK")
    except Exception as exc:  # pragma: no cover — startup probe
        logger.error("initial token acquisition FAILED: %s", exc)
        # Don't exit: a transient TikTok 5xx at boot shouldn't kill the
        # container. First tool call will retry.

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
