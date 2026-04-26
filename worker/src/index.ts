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
});
