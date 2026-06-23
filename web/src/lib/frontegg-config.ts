// Frontegg embedded-login configuration for @frontegg/react (feature 029).
//
// Only PUBLIC identifiers reach the browser bundle: the tenant base URL and the
// public client id. No client_secret / encryption password is used by the
// client-only SPA SDK — see specs/029-google-oauth-embedded/research.md R1/R7.
// The Dockerfile secret guard greps the bundle to enforce this (SC-007).

import type { FronteggThemeOptions } from "@frontegg/react";

// NEXT_PUBLIC_* are inlined at build time. Must be the tenant-scoped Frontegg
// URL (e.g. https://auth.okareo.com), NOT https://api.frontegg.com. Typed by
// inference and validated where it is passed to FronteggProvider.contextOptions.
export const contextOptions = {
    baseUrl: process.env.NEXT_PUBLIC_FRONTEGG_BASE_URL ?? "",
    clientId: process.env.NEXT_PUBLIC_FRONTEGG_CLIENT_ID ?? "",
};

// The page is served under basePath '/login'; the SDK's internal router uses the
// same base so its routes (/account/login, /oauth/callback) resolve to /login/*
// — which the MCP server already serves via its SPA fallback.
//
// The SDK builds the social/OAuth redirect_uri as
//   `${window.location.origin}${basename}${oauthRedirectPath}`  (path strategy)
// so the defaults below produce `${origin}/login/oauth/callback`. Both are
// env-overridable so the redirect target can be matched to whatever is
// registered in the Frontegg portal WITHOUT a logic change — see the redirect
// troubleshooting in specs/029-google-oauth-embedded/quickstart.md.
//
//   Default (recommended):  BASENAME=/login  REDIRECT_PATH=/oauth/callback
//     → redirect_uri = <origin>/login/oauth/callback   (served by /login SPA fallback)
//   If the SDK is NOT prefixing the basename on your build, switch to:
//     NEXT_PUBLIC_FRONTEGG_BASENAME=""  NEXT_PUBLIC_FRONTEGG_OAUTH_REDIRECT_PATH="/login/oauth/callback"
//     (keeps the redirect under /login; note other in-app routes then also need
//      the /login prefix, so prefer the default unless you confirm a mismatch).
export const FRONTEGG_BASENAME =
    process.env.NEXT_PUBLIC_FRONTEGG_BASENAME ?? "/login";

export const FRONTEGG_OAUTH_REDIRECT_PATH =
    process.env.NEXT_PUBLIC_FRONTEGG_OAUTH_REDIRECT_PATH ?? "/oauth/callback";

// Mirror appfrontend's loginBox social-login styling so the embedded box reaches
// visual parity with the main app (SC-006, FR-007). The set of providers shown
// (Google, etc.) is driven entirely by central Frontegg config, not by this file
// (FR-003) — we only style what Frontegg renders.
const socialLogins = {
    containerStyle: { display: "flex" },
    dividerStyle: { display: "flex" },
};

export const loginBoxTheme: FronteggThemeOptions = {
    loginBox: {
        login: { socialLogins },
        signup: { socialLogins },
    },
};
