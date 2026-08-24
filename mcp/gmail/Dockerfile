# Gmail MCP — custom server, no upstream package to wrap.
#
# READ-ONLY access to the Choiz + Timeless shared inboxes (hola@choiz.com.mx,
# hi@gotimeless.ai) over the Gmail REST API. One slug (/mcp/gmail), one
# container, one credential per mailbox — the `account` argument selects the
# mailbox, and account="both" fans out. This deviates from the per-brand slug
# convention (ga4-choiz / powerbi-timeless / ...) on purpose: the entire point
# is answering one question across both inboxes in a single tool call.
#
# Auth: one OAuth refresh token PER MAILBOX, not a service account with
# domain-wide delegation. DWD would make this container able to read every
# mailbox in both domains; a per-mailbox token reads exactly one. See the
# module docstring in entrypoint.py and mint_token.py for how to obtain them.
#
# NOT write-capable and not intended to become so: unlike dhl / sheets there is
# no kill-switch here, because the scope granted is `gmail.readonly` and there
# are no write tools to gate. Inbox content is attacker-controllable (anyone
# can email a shared inbox), so keeping the surface read-only is what bounds
# prompt-injection damage to "bad answer" instead of "action taken".
#
# Shape mirrors dhl / powerbi / viral-loops: FastMCP + REST client,
# stateless_http=True, served in-process over Streamable HTTP. No supergateway
# (dropped stack-wide 2026-05-07).

FROM python:3.12-slim

# ca-certificates: HTTPS to gmail.googleapis.com + oauth2.googleapis.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Pin mcp to 1.25.0 to match the rest of the stack (warehouse / facebook /
# instagram / ga4 / powerbi / tiktok-organic / dhl / viral-loops).
# google-api-python-client ships the bundled gmail v1 discovery document, which
# is what lets entrypoint.py build the client with static_discovery=True (no
# network round-trip, no discovery cache on disk).
RUN pip install --no-cache-dir \
    'mcp==1.25.0' \
    'google-api-python-client>=2.140,<3' \
    'google-auth>=2.30,<3'

# Flush stdout immediately so init logs land in `docker logs` without delay.
ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

ENTRYPOINT ["python", "/opt/entrypoint.py"]
