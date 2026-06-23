import { describe, expect, it } from "vitest";

import {
    contextOptions,
    FRONTEGG_BASENAME,
    FRONTEGG_OAUTH_REDIRECT_PATH,
    loginBoxTheme,
} from "@/lib/frontegg-config";

// US2/US3 guardrails: the MCP must not maintain its own list of sign-in methods
// or suppress sign-up — the set of methods (email/password, Google, future
// providers) is driven entirely by central Frontegg config (FR-003), and the
// embedded box renders them all (FR-005/006/006a). These assertions fail if a
// future change reintroduces an MCP-specific allow-list or hides a method.

describe("frontegg-config (US2/US3 method parity)", () => {
    it("contextOptions carries only public identifiers, no method allow-list", () => {
        expect(Object.keys(contextOptions).sort()).toEqual(["baseUrl", "clientId"]);
    });

    it("does not enumerate or restrict providers (no per-method allow-list)", () => {
        const serialized = JSON.stringify(loginBoxTheme);
        // styling only — never a list of which providers to show/hide
        expect(serialized).not.toMatch(/allowedProviders|providers"\s*:/i);
        expect(serialized).not.toMatch(/disableSignup|hideSignup/i);
    });

    it("styles social logins for both login and signup (parity with main app)", () => {
        expect(loginBoxTheme.loginBox?.login?.socialLogins).toBeDefined();
        expect(loginBoxTheme.loginBox?.signup?.socialLogins).toBeDefined();
    });

    it("uses the /login basename so SDK routes resolve under the served sub-path", () => {
        expect(FRONTEGG_BASENAME).toBe("/login");
    });

    it("defaults the OAuth redirect path to /oauth/callback (→ /login/oauth/callback with basename)", () => {
        expect(FRONTEGG_OAUTH_REDIRECT_PATH).toBe("/oauth/callback");
    });
});
