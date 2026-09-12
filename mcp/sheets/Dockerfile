# Google Sheets MCP — read + write to Google Sheets / Drive
# (get_sheet_data, update_cells, batch_update_cells, add_rows, add_columns,
# create_spreadsheet, create_sheet, copy_sheet, rename_sheet,
# share_spreadsheet, add_chart, list_spreadsheets, search_spreadsheets, ...).
#
# Source: https://github.com/xing5/mcp-google-sheets — public PyPI package
# `mcp-google-sheets`, no Choiz fork. Built on FastMCP
# (mcp.server.fastmcp.FastMCP), same server framework as the official
# ga4 / google-ads MCPs we already run in-process.
#
# Transport: FastMCP speaks Streamable HTTP natively. We do NOT use
# supergateway (dropped stack-wide on 2026-05-07; --stateless leaks,
# --stateful trips supergateway #126 with claude.ai). entrypoint.py imports
# the package's module-level `mcp` FastMCP instance, forces its mount path
# to "/" (FastMCP defaults to "/mcp", but the gateway strips the
# /mcp/<name> prefix and forwards to upstream at "/"), and serves it on
# 0.0.0.0:8080. Same in-process shape as mcp/ga4/.
#
# Auth: dedicated `sheets-editor` service account (NOT the read-only
# ga4-mcp-reader). The package reads CREDENTIALS_CONFIG — a single-line
# base64 of the SA JSON — and builds service_account.Credentials directly,
# so no /tmp file materialization is needed (cleaner than ga4/gsc). The
# SA must have the Sheets + Drive APIs enabled and EACH target spreadsheet
# shared with its client_email as Editor. See compose.yml + .env.example.
#
# Write surface: this is the first read+write MCP in the gateway (warehouse
# is read-only, ga4/gsc are readers). Scope is bounded by what is shared
# with the SA, not by API scope alone.
#
# Pin to a commit of the upstream main branch for reproducibility. Bump
# SHEETS_MCP_VERSION deliberately after testing a newer release.

FROM python:3.12-slim

# Pin the published package version (not a git SHA — this one ships clean
# wheels on PyPI, unlike ga4/google-ads which we install from git).
# 0.6.3 = current release: 20 tools and native ENABLED_TOOLS filtering
# (verified — 0.5.x had only 15 tools and ignored ENABLED_TOOLS, which
# would have silently defeated the write kill-switch).
ARG SHEETS_MCP_VERSION=0.6.3

# ca-certificates: HTTPS to sheets.googleapis.com + www.googleapis.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# `mcp` is pinned EXPLICITLY even though mcp-google-sheets pulls it
# transitively. Outage 2026-08-24: an unrelated compose.yml change rebuilt all 16
# images and every image built on the SDK's FastMCP surface crashlooped —
# warehouse on `mcp.server.fastmcp` disappearing, ga4 on `Tool.inputSchema`
# becoming `Tool.input_schema`, and this one too. The package version was pinned
# but its SDK floor was not, so the image was reproducible only by accident.
# 1.25.0 is the version the rest of the SDK-based stack runs.
RUN pip install --no-cache-dir \
    "mcp-google-sheets==${SHEETS_MCP_VERSION}" \
    "mcp==2.2.0" \
    "uvicorn[standard]"

# Fail the BUILD, not production, if the SDK layout moves again.
# `mcp.server.fastmcp` is the module that vanished on 2026-08-24 and the one this
# package is built on. Deliberately NOT importing mcp_google_sheets.server: that
# builds the FastMCP instance and may want CREDENTIALS_CONFIG, which does not
# exist at build time — a check that can fail for the wrong reason is worse than
# a narrower one.
RUN python -c "import mcp_google_sheets; import importlib.metadata as m; print('package check OK, mcp', m.version('mcp'))"

# Flush stdout immediately so init logs land in `docker logs` without delay.
ENV PYTHONUNBUFFERED=1

# The package resolves its bind host/port from HOST/PORT at import time.
# Pin them here so the FastMCP instance binds where the gateway expects.
# FASTMCP_STREAMABLE_HTTP_PATH is the env fallback for the mount-path
# override the entrypoint also sets programmatically (FastMCP reads
# settings from the FASTMCP_ env prefix). The gateway forwards to "/".
ENV HOST=0.0.0.0 \
    PORT=8080 \
    FASTMCP_STREAMABLE_HTTP_PATH=/

COPY entrypoint.py /opt/entrypoint.py

# Fail the BUILD, not production, if the SDK layout moves again. Executes
# entrypoint.py's module body — which installs the mcp.server.fastmcp stand-in —
# and THEN imports mcp_google_sheets through it, so the shim is exercised here
# rather than by a user. The earlier check imported mcp.server.fastmcp directly;
# that module no longer exists in mcp 2.x, which is the whole reason for the
# shim. Protocol revision 2026-07-28 is asserted too: every mcp 1.x release
# answers it with a bare HTTP 400 that claude.ai reports as "Connection closed".
RUN python -c "\
import importlib.util, importlib.metadata as m; \
from mcp_types.version import KNOWN_PROTOCOL_VERSIONS as K; \
assert '2026-07-28' in K, K; \
spec=importlib.util.spec_from_file_location('e','/opt/entrypoint.py'); \
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); \
import mcp_google_sheets; \
print('import check OK, mcp', m.version('mcp'), K)"

EXPOSE 8080

ENTRYPOINT ["python", "/opt/entrypoint.py"]
