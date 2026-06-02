# DHL Express MCP — custom server, no upstream package to wrap.
#
# Talks to the MyDHL API (DHL Express REST, express.api.dhl.com) over HTTP
# Basic auth. End users authenticate to claude.ai with Google Workspace; they
# never see a DHL login.
#
# Single-tenant (one DHL Express account). Mirrors the tiktok-organic shape:
# FastMCP + requests, stateless_http=True, no SDK — the surface we use is a
# handful of REST endpoints.
#
# WRITE-CAPABLE: create_shipment / create_return_shipment generate real labels.
# DHL_BASE_URL defaults to the MyDHL test environment inside entrypoint.py;
# production requires deliberately overriding it in the EC2 .env.

FROM python:3.12-slim

# ca-certificates: HTTPS to express.api.dhl.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin mcp to 1.25.0 to match the rest of the stack (warehouse / facebook /
# instagram / ga4 / powerbi / tiktok-organic). requests for the DHL HTTP calls.
RUN pip install --no-cache-dir \
    'mcp==1.25.0' \
    'requests>=2.31,<3'

ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

ENTRYPOINT ["python", "/opt/entrypoint.py"]
