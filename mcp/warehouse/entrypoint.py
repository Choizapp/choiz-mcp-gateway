"""Warehouse MCP entrypoint that patches FastMCP to serve at "/" instead of "/mcp".

Why this exists:
  FastMCP (mcp 1.25.0) hard-codes ``streamable_http_path: str = "/mcp"`` as a
  default in its ``__init__`` signature. When postgres-mcp calls
  ``FastMCP(name=...)`` without specifying that kwarg, the function-level
  default propagates into Settings as an explicit kwarg, which has higher
  precedence than environment variables in pydantic-settings — so setting
  ``FASTMCP_STREAMABLE_HTTP_PATH=/`` in the container env does NOT work.

  Without overriding the path, FastMCP serves at ``/mcp`` and issues a 307
  redirect for any request to ``/`` whose Location header references the
  internal Docker hostname (``http://warehouse_mcp:8080/mcp``), which is
  unreachable from claude.ai.

Workaround:
  Replace ``FastMCP.__init__`` in-place to inject ``streamable_http_path="/"``
  as a kwarg default. We must NOT subclass: ``FastMCP`` is a Generic
  (``FastMCP[LifespanResultT]``) and any plain subclass loses the generic
  parameterisation, which breaks postgres-mcp's runtime type evaluation
  with ``TypeError: <class '__main__._FastMCPRootPath'> is not a generic class``
  when pydantic resolves forward refs in field annotations.

Switch back to the upstream CLI when one of these lands:
  - postgres-mcp 0.4.0 with a CLI flag like ``--streamable-http-path``,
  - or mcp SDK changes ``streamable_http_path`` so its env var actually wins.

See: https://github.com/crystaldba/postgres-mcp/blob/07eb329c/src/postgres_mcp/server.py
"""
from __future__ import annotations

import asyncio

import mcp.server.fastmcp as _fastmcp_pkg

_orig_init = _fastmcp_pkg.FastMCP.__init__


def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    # Force streamable-http path "/" (see module docstring).
    kwargs.setdefault("streamable_http_path", "/")
    # Force host "0.0.0.0" so FastMCP's auto-enabled DNS rebinding
    # protection (triggered for localhost/127.0.0.1/::1) does NOT engage.
    # Otherwise transport_security rejects requests whose Host header
    # is "warehouse_mcp:8080" (the value http-proxy-middleware sets when
    # the gateway uses changeOrigin: true) with HTTP 421 Misdirected
    # Request / "Invalid Host header". postgres-mcp overrides
    # settings.host later from --streamable-http-host anyway, so this
    # default is harmless.
    kwargs.setdefault("host", "0.0.0.0")
    # Force stateless sessions. Nothing here needs per-session state, and
    # keeping it costs a manual reconnect on every redeploy: claude.ai keeps
    # sending the Mcp-Session-Id of the replaced container, the SDK answers an
    # unknown session with 400, the spec says 404, and claude.ai only
    # re-initializes on 404 — so the connector stays wedged. Hit on ga4 and
    # sheets on 2026-08-24; this container is the same shape, fixed here before
    # it bites. See memory feedback_stale_session_after_redeploy.
    kwargs.setdefault("stateless_http", True)
    _orig_init(self, *args, **kwargs)


# Replace __init__ in place; the class object stays the same so
# ``FastMCP[X]`` generic parameterisation continues to work.
_fastmcp_pkg.FastMCP.__init__ = _patched_init  # type: ignore[method-assign]

# Now import postgres-mcp; its ``server`` module evaluates
# ``from mcp.server.fastmcp import FastMCP`` at load time and gets the
# (in-place-patched) class.
from postgres_mcp.server import main  # noqa: E402


if __name__ == "__main__":
    asyncio.run(main())
