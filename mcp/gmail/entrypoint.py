"""Gmail MCP — READ-ONLY access to the Choiz + Timeless shared inboxes.

Why this exists: claude.ai's native Gmail connector authenticates as the
signed-in user, one Google account per connector, and cannot be added twice.
Querying hola@choiz.com.mx and hi@gotimeless.ai in the same turn therefore
needs a server-side MCP holding one credential per mailbox.

Auth — one OAuth refresh token PER MAILBOX (deliberately NOT a service
account with domain-wide delegation). DWD would let this container read every
mailbox in both domains, including HR, legal and clinical inboxes; a refresh
token minted while signed in as the shared inbox can read exactly that one
mailbox and nothing else. Cost of that choice: one interactive login per
mailbox at setup time (see mint_token.py). Revisit only if this grows past
~6 mailboxes; the migration note lives in docs/NEXT_STEPS.md.

Config is per-mailbox, not per-Workspace-tenant, so it works unchanged whether
gotimeless.ai is a secondary domain of the Choiz tenant (same client_id/secret
in both blocks) or a separate tenant (a client per GCP org):

    GMAIL_ACCOUNTS=choiz,timeless
    GMAIL_CHOIZ_EMAIL / _CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN
    GMAIL_TIMELESS_EMAIL / _CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN

Scope is `gmail.readonly` and there is intentionally NO write surface — no
send, no reply, no label, no trash, and no attachment download. An inbox is
third-party-controlled content: anyone can email hola@ with text engineered to
be read as instructions. With reads only, the worst case is a bad answer
rather than an action taken on an attacker's behalf. Do not add write tools
here without a deliberate decision (see the DHL/Sheets kill-switch pattern if
that day comes).

Payload discipline (the recurring failure mode of this stack — see memory
project_claudeai_payload_ceiling): email bodies are the largest payloads any
MCP here handles. Defaults return headers + Gmail's own snippet only; full
bodies require include_body=true, are preferred as text/plain, fall back to
tag-stripped HTML, and are hard-capped. Every tool returns compact JSON.

Tool surface (4 tools, read-only):
  - search_messages(query, account, limit)  : Gmail search syntax, headers + snippet
  - get_message(message_id, account, ...)   : one message, body opt-in
  - get_thread(thread_id, account, ...)     : whole conversation, bodies opt-in
  - list_labels(account)                    : label ids/names

`account` accepts "choiz", "timeless" or "both". Message and thread ids are
mailbox-scoped, so get_message/get_thread require ONE account; search_messages
tags every result with the account it came from.
"""
from __future__ import annotations

import base64
import binascii
import functools
import html as html_lib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("gmail_mcp")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# --- Constants ------------------------------------------------------------

# Read-only, full stop. Widening this list is a security decision, not a
# refactor: gmail.readonly is a Google "restricted" scope and the whole
# threat model above depends on it.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
DEFAULT_BODY_CHARS = 4_000
MAX_BODY_CHARS = 20_000
# Long threads: keep the most recent N messages, report how many were omitted.
MAX_THREAD_MESSAGES = 25

# Asking for a metadata-only get with an explicit header list is what keeps
# search results small — Gmail otherwise returns every header (DKIM,
# Received chains, ...), which is mostly noise measured in kilobytes.
METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Reply-To"]


class _UserError(Exception):
    """Bad tool arguments — surfaced to the model as a fixable error, not a 500."""


# --- Account config -------------------------------------------------------


@dataclass
class Account:
    key: str
    email: str
    client_id: str
    client_secret: str
    refresh_token: str
    # googleapiclient's underlying httplib2 object is NOT thread-safe, and
    # FastMCP runs sync tools in a thread pool. One lock per account keeps
    # each mailbox's calls serialized while still letting choiz + timeless
    # run concurrently on an account="both" fan-out.
    lock: threading.Lock = field(default_factory=threading.Lock)
    service: Any = None


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _load_accounts() -> dict[str, Account]:
    """Build the mailbox map from env.

    Three distinct states, treated differently on purpose:

      * a mailbox with NO vars set  -> skipped with a warning. This is the
        normal state between merging this MCP and minting its tokens; a
        crashlooping container on the shared EC2 is worse than a healthy one
        that tells the caller it has nothing wired up yet.
      * a mailbox with SOME vars set -> hard failure. That is a real
        misconfiguration (typo, half-pasted secret), and silently serving one
        mailbox when the operator believes two are live is the bad outcome.
      * no mailboxes at all -> start anyway; every tool returns not_configured.
    """
    keys = [k.strip().lower() for k in _env("GMAIL_ACCOUNTS").split(",") if k.strip()]
    accounts: dict[str, Account] = {}
    for key in keys:
        prefix = f"GMAIL_{key.upper()}_"
        fields = {
            "EMAIL": _env(prefix + "EMAIL"),
            "CLIENT_ID": _env(prefix + "CLIENT_ID"),
            "CLIENT_SECRET": _env(prefix + "CLIENT_SECRET"),
            "REFRESH_TOKEN": _env(prefix + "REFRESH_TOKEN"),
        }
        missing = [prefix + name for name, value in fields.items() if not value]
        if len(missing) == len(fields):
            logger.warning(
                "account '%s' is listed in GMAIL_ACCOUNTS but has no credentials "
                "yet — skipping. Mint them with mcp/gmail/mint_token.py.",
                key,
            )
            continue
        if missing:
            raise RuntimeError(
                f"gmail account '{key}' is PARTIALLY configured — missing: "
                + ", ".join(missing)
                + ". Set all four vars or none."
            )
        accounts[key] = Account(
            key=key,
            email=fields["EMAIL"],
            client_id=fields["CLIENT_ID"],
            client_secret=fields["CLIENT_SECRET"],
            refresh_token=fields["REFRESH_TOKEN"],
        )
    return accounts


ACCOUNTS = _load_accounts()
ACCOUNT_KEYS = sorted(ACCOUNTS)

NOT_CONFIGURED = (
    "no Gmail mailboxes are configured on this server yet — the refresh tokens "
    "have not been minted. Tell the user to run mcp/gmail/mint_token.py and set "
    "GMAIL_<KEY>_* in the EC2 .env; nothing you can do from here will fix it."
)


def _service(acct: Account) -> Any:
    """Build (once) the Gmail client for an account. Call under acct.lock.

    static_discovery=True uses the discovery document bundled in the client
    library, so no network round-trip on first use.
    """
    if acct.service is None:
        creds = Credentials(
            token=None,
            refresh_token=acct.refresh_token,
            client_id=acct.client_id,
            client_secret=acct.client_secret,
            token_uri=TOKEN_URI,
            scopes=SCOPES,
        )
        acct.service = build(
            "gmail",
            "v1",
            credentials=creds,
            cache_discovery=False,
            static_discovery=True,
        )
    return acct.service


def _resolve(account: str) -> list[Account]:
    """Resolve an `account` argument that may fan out to every mailbox."""
    if not ACCOUNTS:
        raise _UserError(NOT_CONFIGURED)
    key = (account or "").strip().lower()
    if key in ("both", "all", "*"):
        return list(ACCOUNTS.values())
    if key in ACCOUNTS:
        return [ACCOUNTS[key]]
    raise _UserError(f"unknown account '{account}'. valid: {ACCOUNT_KEYS} or 'both'")


def _resolve_one(account: str) -> Account:
    """Resolve an `account` argument that must name exactly one mailbox."""
    if not ACCOUNTS:
        raise _UserError(NOT_CONFIGURED)
    key = (account or "").strip().lower()
    if key in ACCOUNTS:
        return ACCOUNTS[key]
    if key in ("both", "all", "*"):
        raise _UserError(
            "this tool needs exactly ONE account — Gmail message and thread ids "
            "are mailbox-scoped, so the same id means nothing in the other "
            f"mailbox. Pass one of {ACCOUNT_KEYS}; search_messages reports the "
            "account each id came from."
        )
    raise _UserError(f"unknown account '{account}'. valid: {ACCOUNT_KEYS}")


# --- Payload helpers ------------------------------------------------------


def _dump(obj: Any) -> str:
    """Compact JSON — no indent, no ASCII escaping. Per the repo's pre-deploy
    checklist: FastMCP's default pretty-printing wastes the payload budget."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _b64(data: str) -> str:
    """Decode Gmail's base64url part data. Never raises — a body we cannot
    decode is worth an empty string, not a failed tool call."""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


_HTML_BLOCKS = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_HTML_BREAKS = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li|/h[1-6])\s*/?>")
_HTML_TAGS = re.compile(r"<[^>]+>")
# \u00a0 (non-breaking space) is what &nbsp; unescapes to, and
# marketing HTML is full of it; without it in this class every body arrives
# peppered with NBSPs. Escapes rather than literals on purpose: invisible
# characters in source are a maintenance trap.
_SPACES = re.compile("[ \t\r\f\v\u00a0]+")
# Zero-width junk (joiners, BOM) used as spacer glyphs in email templates.
_INVISIBLE = re.compile("[\u200b-\u200d\ufeff]")
_BLANK_LINES = re.compile(r"\n{3,}")


def _strip_html(raw: str) -> str:
    """Crude HTML to text. Deliberately dependency-free: the goal is a readable
    body for an LLM, not faithful rendering, and marketing email is 90% markup
    by weight so stripping it is the single biggest payload win available."""
    text = _HTML_BLOCKS.sub(" ", raw)
    text = _HTML_BREAKS.sub("\n", text)
    text = _HTML_TAGS.sub(" ", text)
    text = html_lib.unescape(text)
    text = _INVISIBLE.sub("", text)
    text = _SPACES.sub(" ", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def _walk_parts(payload: dict[str, Any], acc: dict[str, list[str]]) -> None:
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data and mime in ("text/plain", "text/html"):
        acc.setdefault(mime, []).append(_b64(data))
    for part in payload.get("parts") or ():
        _walk_parts(part, acc)


def _body_text(payload: dict[str, Any], max_chars: int) -> str:
    """Best-effort plain text for a message, capped at max_chars."""
    acc: dict[str, list[str]] = {}
    _walk_parts(payload, acc)
    text = "\n".join(acc.get("text/plain") or ()).strip()
    if not text:
        text = _strip_html("\n".join(acc.get("text/html") or ()))
    if len(text) > max_chars:
        omitted = len(text) - max_chars
        return (
            text[:max_chars]
            + f"\n[truncated: {omitted} more chars — raise max_body_chars to see them]"
        )
    return text


def _attachments(
    payload: dict[str, Any], out: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Attachment manifest (name/type/size). Never the content: there is no
    download tool here on purpose — binaries blow the payload ceiling and
    base64 through claude.ai hangs the session (see the DHL label incident)."""
    out = [] if out is None else out
    for part in payload.get("parts") or ():
        body = part.get("body") or {}
        filename = part.get("filename")
        if filename and body.get("attachmentId"):
            out.append(
                {
                    "filename": filename,
                    "mime": part.get("mimeType"),
                    "bytes": body.get("size"),
                }
            )
        _attachments(part, out)
    return out


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    wanted = {"from", "to", "subject", "date"}
    found: dict[str, str] = {}
    for header in (msg.get("payload") or {}).get("headers") or ():
        name = (header.get("name") or "").lower()
        if name in wanted and name not in found:
            found[name] = header.get("value") or ""
    return found


def _summarize(acct: Account, msg: dict[str, Any]) -> dict[str, Any]:
    hdrs = _headers(msg)
    labels = msg.get("labelIds") or []
    return {
        "account": acct.key,
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "date": hdrs.get("date"),
        "from": hdrs.get("from"),
        "to": hdrs.get("to"),
        "subject": hdrs.get("subject"),
        # Gmail's own ~200-char preview. Cheap, and usually enough to decide
        # whether a full get_message is worth it.
        "snippet": html_lib.unescape(msg.get("snippet") or ""),
        "unread": "UNREAD" in labels,
        # Epoch millis — used to merge/sort a two-mailbox fan-out.
        "internal_ts": int(msg.get("internalDate") or 0),
    }


def _guard(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn exceptions into compact JSON errors the model can act on.

    Nothing here logs message content, subjects or search queries: the whole
    point of this MCP is inboxes that may carry patient correspondence, and
    `docker logs` is not the place for it.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except _UserError as exc:
            return _dump({"error": "bad_argument", "detail": str(exc)})
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            logger.error("%s: gmail api error status=%s", fn.__name__, status)
            hint = None
            if status in (401, 403):
                hint = (
                    "the mailbox's refresh token may be revoked or the OAuth app "
                    "un-trusted in Admin Console; re-mint with mint_token.py"
                )
            return _dump(
                {
                    "error": "gmail_api_error",
                    "status": status,
                    "detail": getattr(exc, "reason", None) or "request rejected",
                    **({"hint": hint} if hint else {}),
                }
            )
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            logger.exception("%s: unhandled error", fn.__name__)
            return _dump({"error": "internal_error", "detail": str(exc)[:200]})

    return wrapper


# --- MCP server -----------------------------------------------------------

# host="0.0.0.0" so the gateway reaches us across the docker bridge.
# streamable_http_path="/" so the gateway can strip /mcp/gmail and forward "/".
# stateless_http=True: ephemeral sessions, sidesteps stale-session-after-redeploy.
mcp = FastMCP(
    name="gmail",
    instructions=(
        "Read-only access to Choiz's shared inboxes: "
        + (
            ", ".join(f"{k} ({a.email})" for k, a in ACCOUNTS.items())
            or "(none configured yet — every tool will say so)"
        )
        + ". Every tool takes an `account` argument; search_messages and "
        "list_labels also accept account='both' to cover all mailboxes in one "
        "call. `query` uses native Gmail search syntax (from:, to:, subject:, "
        "is:unread, has:attachment, after:2026/08/01, label:...). Start with "
        "search_messages — it returns headers plus a snippet, which usually "
        "answers the question; only call get_message/get_thread with "
        "include_body=true when the actual text matters. This server cannot "
        "send, reply, label, delete, or download attachments."
    ),
    host="0.0.0.0",
    port=8080,
    streamable_http_path="/",
    stateless_http=True,
)


def _search_one(acct: Account, query: str, limit: int) -> list[dict[str, Any]]:
    """List + metadata-fetch for one mailbox.

    N+1 by construction: messages.list returns ids only, so each result needs
    its own metadata get. At the default limit of 10 that is ~10 requests over
    one reused connection (~1s). If limits ever climb, batch these — but batch
    adds a failure mode for a latency win we do not currently need.
    """
    with acct.lock:
        svc = _service(acct)
        listed = (
            svc.users()
            .messages()
            .list(userId="me", q=query or None, maxResults=limit)
            .execute()
        )
        rows: list[dict[str, Any]] = []
        for stub in listed.get("messages") or ():
            msg = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=stub["id"],
                    format="metadata",
                    metadataHeaders=METADATA_HEADERS,
                )
                .execute()
            )
            rows.append(_summarize(acct, msg))
    return rows


@mcp.tool()
@_guard
def search_messages(
    query: str = "",
    account: str = "both",
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Search one or both shared inboxes. Returns headers + snippet, no bodies.

    `query` is native Gmail search syntax — the same string you would type in
    the Gmail search box. Examples: "is:unread", "from:proveedor@x.com",
    "subject:factura after:2026/08/01", "has:attachment newer_than:7d".
    Empty query = most recent mail (excludes Spam and Trash).

    `account`: "choiz", "timeless", or "both" (default). With "both", results
    from the two mailboxes are merged and sorted newest-first, and each row
    carries the `account` it came from — you need that value to call
    get_message or get_thread on it.

    `limit` caps results per mailbox before merging (default 10, max 50).

    Snippets are Gmail's ~200-char preview. Call get_message with
    include_body=true only when the snippet is not enough.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    accounts = _resolve(account)
    rows: list[dict[str, Any]] = []
    for acct in accounts:
        rows.extend(_search_one(acct, query, limit))
    rows.sort(key=lambda r: r["internal_ts"], reverse=True)
    truncated = len(rows) > limit
    rows = rows[:limit]
    logger.info(
        "search_messages: accounts=%s returned=%d truncated=%s",
        [a.key for a in accounts],
        len(rows),
        truncated,
    )
    return _dump(
        {
            "count": len(rows),
            "limit": limit,
            "truncated": truncated,
            "messages": rows,
        }
    )


@mcp.tool()
@_guard
def get_message(
    message_id: str,
    account: str,
    include_body: bool = False,
    max_body_chars: int = DEFAULT_BODY_CHARS,
) -> str:
    """Fetch one message by id from ONE mailbox.

    `account` must name a single mailbox ("choiz" or "timeless") — ids are
    mailbox-scoped. Take both values from a search_messages result.

    `include_body=false` (default) returns headers + snippet + labels only.
    Set it to true for the actual text: plain-text part preferred, HTML
    stripped to text as a fallback, capped at `max_body_chars` (default 4000,
    max 20000). The attachment manifest (names/types/sizes — never content)
    also requires include_body=true.
    """
    acct = _resolve_one(account)
    max_body_chars = max(200, min(int(max_body_chars), MAX_BODY_CHARS))
    params: dict[str, Any] = {
        "userId": "me",
        "id": message_id,
        "format": "full" if include_body else "metadata",
    }
    if not include_body:
        params["metadataHeaders"] = METADATA_HEADERS
    with acct.lock:
        msg = _service(acct).users().messages().get(**params).execute()

    out = _summarize(acct, msg)
    out["labels"] = msg.get("labelIds") or []
    payload = msg.get("payload") or {}
    if include_body:
        out["body"] = _body_text(payload, max_body_chars)
        attachments = _attachments(payload)
        if attachments:
            out["attachments"] = attachments
    logger.info(
        "get_message: account=%s body=%s chars=%d",
        acct.key,
        include_body,
        len(out.get("body") or ""),
    )
    return _dump(out)


@mcp.tool()
@_guard
def get_thread(
    thread_id: str,
    account: str,
    include_bodies: bool = False,
    max_body_chars: int = 2_000,
) -> str:
    """Fetch a whole conversation by thread id from ONE mailbox.

    Use this to read a back-and-forth in order instead of pulling messages one
    by one. `account` must name a single mailbox; take it and the thread_id
    from a search_messages result.

    `include_bodies=false` (default) returns one header+snippet row per
    message. With true, each message also carries its text, capped at
    `max_body_chars` (default 2000 per message — lower than get_message's
    because a thread multiplies it).

    Long threads return only the 25 most recent messages; `omitted_oldest`
    tells you how many were left out.
    """
    acct = _resolve_one(account)
    max_body_chars = max(200, min(int(max_body_chars), MAX_BODY_CHARS))
    params: dict[str, Any] = {
        "userId": "me",
        "id": thread_id,
        "format": "full" if include_bodies else "metadata",
    }
    if not include_bodies:
        params["metadataHeaders"] = METADATA_HEADERS
    with acct.lock:
        thread = _service(acct).users().threads().get(**params).execute()

    messages = thread.get("messages") or []
    # Keep the NEWEST messages: Gmail returns threads oldest-first, so slicing
    # from the front would drop exactly the part the user is asking about.
    kept = messages[-MAX_THREAD_MESSAGES:]
    rows: list[dict[str, Any]] = []
    for msg in kept:
        row = _summarize(acct, msg)
        if include_bodies:
            row["body"] = _body_text(msg.get("payload") or {}, max_body_chars)
        rows.append(row)
    logger.info(
        "get_thread: account=%s messages=%d bodies=%s",
        acct.key,
        len(rows),
        include_bodies,
    )
    return _dump(
        {
            "account": acct.key,
            "thread_id": thread_id,
            "message_count": len(messages),
            "omitted_oldest": len(messages) - len(kept),
            "messages": rows,
        }
    )


@mcp.tool()
@_guard
def list_labels(account: str = "both") -> str:
    """List the labels of one or both mailboxes (id, name, system vs user).

    Useful before searching with `label:<name>`, and to discover how a shared
    inbox is organized (triage labels, per-topic folders).
    """
    out: list[dict[str, Any]] = []
    for acct in _resolve(account):
        with acct.lock:
            listed = _service(acct).users().labels().list(userId="me").execute()
        for label in listed.get("labels") or ():
            out.append(
                {
                    "account": acct.key,
                    "id": label.get("id"),
                    "name": label.get("name"),
                    "type": label.get("type"),
                }
            )
    return _dump({"count": len(out), "labels": out})


def main() -> None:
    if not ACCOUNTS:
        logger.warning(
            "gmail mcp starting with ZERO mailboxes configured — the route will "
            "answer, but every tool returns not_configured until GMAIL_<KEY>_* "
            "are set in the EC2 .env (see mcp/gmail/mint_token.py)."
        )
    else:
        logger.info(
            "gmail mcp starting — read-only, %d mailbox(es): %s",
            len(ACCOUNTS),
            ", ".join(f"{k}={a.email}" for k, a in ACCOUNTS.items()),
        )
    # Auth probe per mailbox. Deliberately non-fatal: a revoked token should
    # produce a loud log line and a clean per-call error, not a crashloop that
    # takes the other (working) mailbox down with it.
    for key, acct in ACCOUNTS.items():
        try:
            with acct.lock:
                profile = _service(acct).users().getProfile(userId="me").execute()
            actual = (profile.get("emailAddress") or "").lower()
            if actual != acct.email.lower():
                logger.warning(
                    "account '%s': token belongs to %s but GMAIL_%s_EMAIL says %s "
                    "— the token was probably minted while signed in as the wrong "
                    "user; this MCP will read %s",
                    key,
                    actual,
                    key.upper(),
                    acct.email,
                    actual,
                )
            else:
                logger.info(
                    "account '%s': auth ok (%s messages in mailbox)",
                    key,
                    profile.get("messagesTotal"),
                )
        except Exception as exc:  # noqa: BLE001 - probe must not kill startup
            logger.error(
                "account '%s': AUTH PROBE FAILED (%s) — tools will return "
                "gmail_api_error until the refresh token is fixed",
                key,
                exc,
            )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
