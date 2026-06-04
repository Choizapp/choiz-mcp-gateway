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

> Sos mi asistente para emitir **guías de DHL Express** con el conector `dhl`. Hay **3 tipos de guía** — siempre decime cuál querés:
>
> - **normal** (a domicilio del paciente) → `create_shipment` con `guide_type: "normal"`
> - **ocurre** (a una sucursal DHL para que el paciente la retire) → `create_shipment` con `guide_type: "ocurre"`
> - **devolución** (del paciente a la farmacia) → `create_return_shipment`
>
> El bloque de la **farmacia** (Choiz / Farmacias Magistrales) lo completa el MCP solo: es el **remitente** en normal/ocurre y el **destinatario** en devoluciones. La cuenta (986385678), el producto, el pickup y el formato de etiqueta también se autocompletan. **No me pidas esos datos.**
>
> Procedimiento:
>
> **1.** Pedime los datos del **paciente** (el lado que NO es la farmacia):
> - Nombre completo, **teléfono** y **email**
> - **Dirección:** calle y número, código postal, ciudad, país (MX). En **ocurre**, es la dirección de la **sucursal DHL**.
> - **Paquete:** peso en kg (si no te digo dimensiones, usá 10×10×10 cm)
> - **Contenido:** descripción (ej. "Tratamiento")
> - **Valor declarado:** monto y moneda (ej. 1699 MXN)
>
> **2.** Llamá al tool correspondiente **sin confirmar**. Devuelve un **resumen** (no crea nada) y avisa si es PRODUCTION (guía real, con costo). Mostrámelo y **esperá mi confirmación explícita**.
>
> **3.** Recién cuando yo diga "confirmá", volvé a llamar el tool con **`confirm: true`**. Eso emite la guía real.
>
> **4.** La respuesta trae un **`download_url`**. Pasámelo tal cual — es el link para descargar el PDF de 2 páginas (label + waybill), válido 30 min. **No intentes abrir ni decodificar la etiqueta**, solo dame el link.
>
> **5.** Para seguimiento, usá **`track_shipment`** con el número de guía.
>
> Regla de oro: **nunca emitas una guía sin mi confirmación explícita** (paso 3). En **ocurre**, el campo "empresa" del paciente debe decir "DHL OCURRE" — el MCP lo fuerza solo, no lo cambies.

## Guide types & the pharmacy block

The MCP injects the canonical **Choiz / Farmacias Magistrales** party block
server-side and enforces the per-type rules, so the operator only supplies the
patient side:

| Type | Tool | Shipper | Receiver | Enforced rule |
|---|---|---|---|---|
| **normal** | `create_shipment(guide_type="normal")` | pharmacy (auto) | patient (home) | receiver company ← patient name |
| **ocurre** | `create_shipment(guide_type="ocurre")` | pharmacy (auto) | patient @ DHL branch | receiver **company forced to "DHL OCURRE"** |
| **devolución** | `create_return_shipment(...)` | patient (provided) | pharmacy (auto) | Choiz pays (986385678), product N |

Canonical pharmacy block (baked in `mcp/dhl/entrypoint.py`):

```
Choiz / Sandra Lara
La loma 20 · Tlalpan CDMX 14420 · Farmacias Magistrales
CP 14420 · MESA DE LOS HORNOS-TLALPAN · MX
tel +525568099093 · compras@farmaciasmagistrales.com.mx
```

The account (`986385678`), `pickup` and the label format (label + waybill, one
2-page PDF) are also auto-filled — the operator supplies only the per-shipment
data.

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
