# Viral Loops MCP — custom server, no upstream package to wrap.
#
# Talks to the Viral Loops Web API v3 (app.viral-loops.com/api/v3) over HTTPS
# with a per-campaign `apiToken` header. End users authenticate to claude.ai
# with Google Workspace; they never see a Viral Loops login.
#
# READ-ONLY: only GET endpoints are exposed (campaign info/stats + participant
# data/referrals/rank/order/referrer/rewards). Mirrors the dhl / tiktok-organic
# shape: FastMCP + requests, stateless_http=True, no SDK.

FROM python:3.12-slim

# ca-certificates: HTTPS to app.viral-loops.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin mcp to 1.25.0 to match the rest of the stack (warehouse / facebook /
# instagram / ga4 / powerbi / tiktok-organic / dhl). requests for the HTTP calls.
RUN pip install --no-cache-dir \
    'mcp==1.25.0' \
    'requests>=2.31,<3'

ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

ENTRYPOINT ["python", "/opt/entrypoint.py"]
