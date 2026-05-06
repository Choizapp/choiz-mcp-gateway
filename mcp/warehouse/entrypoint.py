"""Warehouse MCP entrypoint that patches FastMCP to serve at "/" instead of "/mcp".

Why this exists:
  FastMCP (mcp 1.25.0) hard-codes ``streamable_http_path: str = "/mcp"`` as a
  default in its ``__init__`` signature. When postgres-mcp calls
  ``FastMCP(name=...)`` without specifying that kwarg, the function-level
  default propagates into Settings as an explicit kwarg, which has higher
  precedence than environment variables in pydantic-settings — so setting
  ``FASTMCP_STREAMABLE_HTTP_PATH=/`` in the container env does NOT work.

  Without overriding the path, FastMCP serves at ``/mcp`` and issues a 307
  redirect for any request to ``/`` or ``/mcp/`` whose Location header
  references the internal Docker hostname (``http://warehouse_mcp:8080/mcp``),
  which is unreachable from claude.ai.

Workaround:
  Subclass ``FastMCP`` and inject ``streamable_http_path="/"`` as a kwarg
  default. Patch the symbol on BOTH the source module (``mcp.server.fastmcp.server``)
  and the package re-export (``mcp.server.fastmcp``) before postgres-mcp imports
  ``FastMCP`` by name. Then defer to ``postgres_mcp.server.main()``.

Switch back to the upstream CLI when one of these lands:
  - postgres-mcp 0.4.0 with a CLI flag like ``--streamable-http-path``,
  - or mcp SDK changes ``streamable_http_path`` to be env-overridable.

See: https://github.com/crystaldba/postgres-mcp/blob/07eb329c/src/postgres_mcp/server.py
"""
from __future__ import annotations

import asyncio

import mcp.server.fastmcp as _fastmcp_pkg
from mcp.server.fastmcp import server as _fastmcp_server_mod

# Capture the original class before any other module imports it by name.
_OrigFastMCP = _fastmcp_pkg.FastMCP


class _FastMCPRootPath(_OrigFastMCP):
    """FastMCP that defaults ``streamable_http_path`` to ``/``."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("streamable_http_path", "/")
        super().__init__(*args, **kwargs)


# Patch BOTH the source module and the package-level re-export. Python's
# ``from mcp.server.fastmcp import FastMCP`` looks up the attribute on the
# package object, which is set at __init__.py load time from the original
# source module. So patching only the source module is not enough — any code
# that does ``from mcp.server.fastmcp import FastMCP`` would still get the
# pre-patch symbol because __init__.py already cached it.
_fastmcp_server_mod.FastMCP = _FastMCPRootPath  # type: ignore[misc]
_fastmcp_pkg.FastMCP = _FastMCPRootPath  # type: ignore[misc]

# Now import postgres-mcp; its ``server`` module evaluates
# ``from mcp.server.fastmcp import FastMCP`` at load time, which reads the
# (now patched) symbol off the package.
from postgres_mcp.server import main  # noqa: E402


if __name__ == "__main__":
    asyncio.run(main())
