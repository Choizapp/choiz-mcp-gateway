import { OAuthProvider } from "@cloudflare/workers-oauth-provider";
import ApiHandler from "./api-handler.js";
import DefaultHandler from "./default-handler.js";

/**
 * Entry point. `OAuthProvider` is a Worker that:
 *   - exposes standard OAuth 2.0 endpoints (/authorize, /token, /register for
 *     Dynamic Client Registration, plus the /.well-known metadata documents
 *     that claude.ai reads to auto-configure)
 *   - validates bearer tokens on /mcp/* requests before handing them to
 *     `apiHandler`
 *   - delegates the actual login UX to `defaultHandler`
 *
 * Secrets live in wrangler secrets, not here.
 */
export default new OAuthProvider({
  apiRoute: "/mcp/",
  apiHandler: ApiHandler as any,
  defaultHandler: DefaultHandler as any,
  authorizeEndpoint: "/authorize",
  tokenEndpoint: "/token",
  clientRegistrationEndpoint: "/register",

  // Workers KV on the free plan allows 1,000 WRITES per day (reads are
  // 100,000, which is why only the write paths ever broke). Every access-token
  // issuance is one KV write — `OAUTH_KV.put("token:<user>:<grant>:<id>", ...)`
  // in the library — and the library's default TTL is 3600s, so every active
  // grant burns 24 writes a day just refreshing.
  //
  // Measured 2026-09-11 via the Cloudflare GraphQL analytics API: daily writes
  // crossed 1,000 on 2026-08-18 and have sat at 1,000-1,330 since, except on
  // weekends. Once the cap is hit, /register and /authorize (the two write
  // paths) return HTTP 500 and NOBODY can connect or reconnect a connector —
  // that is ops issue #62, open and misdiagnosed since 2026-08-24.
  //
  // 24h cuts refresh writes 24x (~1,200/day -> ~50/day), which leaves an order
  // of magnitude of headroom to keep adding MCPs without paying for Workers
  // Paid. The cost is that a leaked access token stays valid for up to a day
  // instead of an hour; acceptable here because every token is already bound to
  // a Google Workspace identity in ALLOWED_EMAIL_DOMAINS and only works against
  // our own /mcp routes. Lower this if that tradeoff ever stops being true.
  accessTokenTTL: 60 * 60 * 24,
});
