# Power BI MCP — custom server, no upstream package to wrap.
#
# Talks to Power BI Service via the REST API endpoint
# /datasets/{id}/executeQueries with a Service Principal (client_credentials).
# This means the auth is fully server-side: end users only authenticate to
# claude.ai with Google Workspace; they never see a Microsoft login prompt.
#
# Why REST + SP instead of XMLA:
#   - PPU includes executeQueries with SP support (no RLS/SSO on our models —
#     verified via POC 2026-05-19, see project_powerbi_mcp_poc_2026-05-19).
#   - Pure HTTP — no ADOMD.NET / .NET runtime needed on ARM64 Linux.
#   - Container stays slim (~100 MB resident, same shape as GA4/GSC).
#   - Limits (100K rows / 1M values / 120 qpm) are fine for LLM-driven Q&A.
#
# Single container, multi-dataset by slug: PBI_DATASET_CHOIZ + PBI_DATASET_TIMELESS
# map to the "choiz" / "timeless" slug arg on each tool. This diverges from
# the per-container per-tenant convention (ga4-choiz / ga4-timeless) because
# both semantic models live under the SAME service principal in the SAME
# workspace; spinning up two identical containers would only add memory cost
# with no separation benefit.

FROM python:3.12-slim

# ca-certificates: HTTPS to login.microsoftonline.com + api.powerbi.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin mcp to 1.25.0 to match the rest of the stack (warehouse/facebook/
# instagram/ga4). msal handles token caching + silent refresh in-process
# so the SP token re-mints transparently before the 1-hour Azure AD TTL.
RUN pip install --no-cache-dir \
    'mcp==1.25.0' \
    'msal>=1.28,<2' \
    'requests>=2.31,<3'

ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

ENTRYPOINT ["python", "/opt/entrypoint.py"]
