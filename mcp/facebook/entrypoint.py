"""Facebook MCP entrypoint — serves FastMCP via streamable-http directly.

Mirror of mcp/warehouse/entrypoint.py. See that file's module docstring for the
full rationale; the short version is:

  - FastMCP (mcp 1.25.0) hard-codes ``streamable_http_path: str = "/mcp"`` and
    ``host: str = "127.0.0.1"`` as kwarg defaults in ``__init__``. Those kwarg
    defaults beat env vars in pydantic-settings, so we cannot configure the
    path/host through the environment.
  - We must NOT subclass FastMCP — it is ``Generic[LifespanResultT]`` and a
    plain subclass breaks generic parameterisation, crashing forward-ref
    resolution at runtime (validated and reverted in PR #8).
  - Therefore we monkey-patch ``FastMCP.__init__`` in place before importing
    the user's ``server.py``.

The fork's server.py only does ``mcp = FastMCP("FacebookMCP")`` and registers
tools with @mcp.tool(); it never calls ``mcp.run()``. The previous image
launched it through ``supergateway --stdio "mcp run /app/server.py"``, which
caused a child-process leak under load (supergateway --stateless spawns a
Python child per request and never reaps them). We now import the FastMCP
instance directly and run it via streamable-http.
"""
from __future__ import annotations

import sys

import mcp.server.fastmcp as _fastmcp_pkg

_orig_init = _fastmcp_pkg.FastMCP.__init__


def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    # Path "/" so the gateway (which strips /mcp/<slug>) reaches the server
    # without a 307 redirect.
    kwargs.setdefault("streamable_http_path", "/")
    # Host 0.0.0.0 disables FastMCP's auto DNS-rebinding protection, which
    # otherwise rejects the "facebook_<tenant>_mcp:8080" Host header that
    # http-proxy-middleware (changeOrigin: true) sends → HTTP 421.
    kwargs.setdefault("host", "0.0.0.0")
    # Listen on 8080 to match the gateway's UPSTREAM_FACEBOOK_* URLs.
    kwargs.setdefault("port", 8080)
    _orig_init(self, *args, **kwargs)


_fastmcp_pkg.FastMCP.__init__ = _patched_init  # type: ignore[method-assign]

# /app holds the cloned fork; make it importable regardless of CWD.
sys.path.insert(0, "/app")

# Import for side effect: builds the FastMCP instance and registers tools.
from server import mcp  # noqa: E402


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
