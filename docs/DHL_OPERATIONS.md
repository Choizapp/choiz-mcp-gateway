# DHL Express MCP — operations guide

The `dhl` MCP (`https://mcp.choiz.com.mx/mcp/dhl/`) lets the team track shipments
and **create DHL Express guides** (labels) from claude.ai. It is the only
**write-capable** MCP in the gateway, so it has extra safeguards.

It wraps the **MyDHL API** over HTTP Basic auth. Same credentials work on both
the test and production bases — the only difference is `DHL_BASE_URL`.

## Tools

| Tool | Type | Cost |
|---|---|---|
| `track_shipment(tracking_number)` | read | none |
| `validate_address(country_code, postal_code, …)` | read | none |
| `get_rates(payload)` | read (quote) | none |
| `create_shipment(payload, confirm)` | **write** | **billable in prod** |
| `create_return_shipment(payload, confirm)` | **write** | **billable in prod** |

Tracking/validate/rates never cost anything. Creating a guide books a real
waybill against the DHL account and bills the freight **only in production with
`confirm=true`** (see below).

## How an operator (e.g. Cami) creates a guide

Paste this at the start of a **new** claude.ai conversation that has the `dhl`
connector enabled:

> Sos mi asistente para emitir **guías de DHL Express** con el conector `dhl`. Seguí siempre este procedimiento:
>
> **1.** Para cada guía usá estos datos (pedímelos si falta alguno):
> - **Destinatario:** nombre completo, **teléfono** y **email**
> - **Dirección destino:** calle y número, código postal, ciudad, estado, país (MX)
> - **Paquete:** peso en kg (si no te digo dimensiones, usá 10×10×10 cm)
> - **Contenido:** descripción (ej. "Tratamiento alopecia masculina")
> - **Valor declarado:** monto y moneda (ej. 1699 MXN)
>
> El **remitente** es Choiz — Sandra Lara, La loma 20, Tlalpan, CDMX 14420, tel +525568099093. **Producto: `N` (EXPRESS DOMESTIC).** La cuenta DHL, el formato de etiqueta y el pickup se completan solos: **no me los pidas**.
>
> **2.** Cuando tengas los datos, llamá a **`create_shipment`**. La primera vez devuelve un **resumen para confirmar** (no crea nada todavía) y avisa que es PRODUCTION (guía real, con costo). Mostrámelo y **esperá mi confirmación explícita**.
>
> **3.** Recién cuando yo diga "confirmá", volvé a llamar **`create_shipment` con `confirm: true`**. Eso emite la guía real.
>
> **4.** La respuesta trae un **`download_url`**. Pasámelo tal cual — es el link para descargar el PDF de 2 páginas (label + waybill), válido 30 min. **No intentes abrir ni decodificar la etiqueta vos**, solo dame el link.
>
> **5.** Para seguimiento, usá **`track_shipment`** con el número de guía.
>
> Regla de oro: **nunca emitas una guía sin mi confirmación explícita** (paso 3).

The MCP auto-fills the shipper account (`986385678`), the label format
(label + "Hand to Courier" waybill, one 2-page PDF) and `pickup`
(`isRequested:false`), so the operator only supplies the per-shipment data.

## When does it cost money?

A real, billable guide is produced **only when all four hold**:

1. `DHL_BASE_URL` points at production (`https://express.api.dhl.com/mydhlapi`).
2. `DHL_ALLOW_CREATE=true`.
3. `create_shipment` (or `create_return_shipment`) is called with **`confirm=true`**.
4. DHL returns the created shipment (a waybill is booked → freight billed to `986385678`).

Free / no cost: tracking, address validation, rate quotes, the first
(un-confirmed) create call, **anything on the test base**, and anything while
the kill-switch is off.

## Kill-switch (emergency brake)

To **stop all guide creation instantly** (reads keep working):

```bash
# On the EC2 over SSM:
cd ~/choiz-mcp-gateway
nano .env                       # set: DHL_ALLOW_CREATE=false
docker compose up -d dhl_mcp    # add sudo if permission denied
```

**Validate it worked** (functional test, definitive): in claude.ai, ask to
create a guide — `create_shipment` returns `status: "disabled"`
(*"Creating a shipment is disabled … DHL_ALLOW_CREATE is off"*) and **no DHL
call happens**.

Re-enable: `DHL_ALLOW_CREATE=true` + `docker compose up -d dhl_mcp`.

## Environment (`.env` on the EC2)

| Var | Meaning |
|---|---|
| `DHL_API_KEY` / `DHL_API_SECRET` | MyDHL API Basic-auth credentials (work on test + prod) |
| `DHL_BASE_URL` | blank = test (`/mydhlapi/test`, SAMPLE labels, $0). `https://express.api.dhl.com/mydhlapi` = production (real, billable) |
| `DHL_ALLOW_CREATE` | `true` to allow `create_*`; `false` (or unset) = kill-switch engaged |
| `DHL_ACCOUNT_NUMBER` | `986385678` — auto-injected as the shipper/payer account |
| `LABEL_DOWNLOAD_BASE_URL` | optional; default `https://mcp.choiz.com.mx/dl/dhl` |
| `LABEL_TTL_MINUTES` | optional; default `30` |

Apply any `.env` change with `docker compose up -d dhl_mcp`, then confirm the
mode in the logs:

```bash
docker compose logs dhl_mcp --tail=5 | grep "dhl mcp starting"
# TEST:  base=https://express.api.dhl.com/mydhlapi/test (TEST)
# PROD:  base=https://express.api.dhl.com/mydhlapi (PRODUCTION) + a WARNING line
```

## How label download works

DHL returns the label as base64 (tens of KB) — too big to return through
claude.ai's tool-result channel (it hangs the session). So the MCP stashes the
PDF in memory under an unguessable token and returns a `download_url`:

```
browser → mcp.choiz.com.mx/dl/dhl/<token>
  → Worker (public, no bearer; token is the capability)
    → gateway /dl/dhl (shared-secret only)
      → dhl_mcp GET /download/<token> → PDF (Content-Disposition: attachment)
```

The store is in-memory only: **links are lost on a container restart** and
expire after `LABEL_TTL_MINUTES`. Labels are ephemeral — recreate the guide or
reprint from the MyDHL+ portal if a link expired.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "The connector isn't responding" right after a deploy | The claude.ai connector holds a stale MCP session (the container was recreated). **Retry once** — it re-initializes. |
| `create_shipment` returns `status: "disabled"` | Kill-switch is off (`DHL_ALLOW_CREATE` not truthy). Enable it in `.env` + restart. |
| `create_shipment` returns `confirmation_required` | Expected — that's the safety step. Re-call with `confirm=true` after the operator approves. |
| Label has a "DO NOT PRINT – SAMPLE ONLY" watermark | You are on the **test** base. Set `DHL_BASE_URL` to production. |
| DHL error `422 required key [pickup]` | Should not happen (auto-injected). If a custom payload overrides `pickup` with something invalid, fix the payload. |
| Guide is 1 page / missing waybill / empty contact | The payload omitted data. `outputImageProperties` is auto-filled for the 2-page label; contact (phone/email) and `declaredValue` must be in the payload. |

See the `project_dhl_mcp_scaffold_2026-06-02` memory and PRs #35–#41 for the
build history.
