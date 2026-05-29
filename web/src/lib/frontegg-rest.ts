// Direct Frontegg identity-API client (no Frontegg SDK).
//
// Per specs/021-embedded-login/research.md R2: v1 of the embedded login uses
// Frontegg's REST endpoints directly so we can render the form in Mantine
// without depending on @frontegg/react. Stretch flows (MFA, password reset)
// can drop in @frontegg/react later if needed.
//
// Endpoints:
//   - POST /identity/resources/auth/v1/user        — sign in
//   - POST /identity/resources/users/v1/signUp     — sign up (creates user + tenant)
//
// Error mapping is intentionally non-leaky: bad-credentials and unknown-email
// both surface as `invalid_credentials` so we don't disclose account existence
// (FR-012).

export type AuthSuccess = {
    kind: "tokens";
    accessToken: string;
    refreshToken: string;
    expiresIn: number;
};

export type SignUpVerificationPending = {
    kind: "verification_pending";
};

export type AuthError =
    | { kind: "invalid_credentials" }
    | { kind: "signup_conflict" }
    | { kind: "verification_pending" }
    | { kind: "mfa_required"; mfaToken: string }
    | { kind: "frontegg_unavailable" }
    | { kind: "unknown_error"; status?: number };

export type SignInResult = AuthSuccess | AuthError;
export type SignUpResult = AuthSuccess | SignUpVerificationPending | AuthError;

// Frontegg tenant base URL — MUST be set at build time via the
// NEXT_PUBLIC_FRONTEGG_BASE_URL env var (web/.env or build-arg). Falling
// back to `https://api.frontegg.com` (the multi-tenant aggregator) silently
// is dangerous: tenants are not addressable on that hostname and the call
// gets 401'd with no Frontegg-side log, which is exactly the failure we saw
// in the first round of demo testing. Better to fail loudly.
const RAW_BASE_URL = process.env.NEXT_PUBLIC_FRONTEGG_BASE_URL ?? "";
const BASE_URL = RAW_BASE_URL.replace(/\/$/, "");

function _assertBaseUrlConfigured(): boolean {
    if (!BASE_URL || BASE_URL === "https://api.frontegg.com") {
        // eslint-disable-next-line no-console
        console.error(
            "[frontegg-rest] NEXT_PUBLIC_FRONTEGG_BASE_URL is not set to a " +
                "tenant-scoped Frontegg URL. The page cannot authenticate. " +
                "Set it to your Frontegg tenant subdomain (e.g. " +
                "https://auth.<your-domain>.com or https://app-xxxx.frontegg.com) " +
                "in web/.env (or as a Docker build-arg), then rebuild the page. " +
                `Current value: ${JSON.stringify(RAW_BASE_URL)}`,
        );
        return false;
    }
    return true;
}

type FronteggTokenResponse = {
    accessToken?: string;
    access_token?: string;
    refreshToken?: string;
    refresh_token?: string;
    expiresIn?: number;
    expires_in?: number;
    mfaToken?: string;
    mfa_token?: string;
};

function normalizeTokens(payload: FronteggTokenResponse): AuthSuccess | null {
    const accessToken = payload.accessToken ?? payload.access_token;
    const refreshToken = payload.refreshToken ?? payload.refresh_token;
    const expiresIn = payload.expiresIn ?? payload.expires_in;
    if (!accessToken || !refreshToken || typeof expiresIn !== "number") {
        return null;
    }
    return { kind: "tokens", accessToken, refreshToken, expiresIn };
}

async function postFrontegg(
    path: string,
    body: Record<string, unknown>,
): Promise<{ ok: boolean; status: number; json: FronteggTokenResponse | Record<string, unknown> }> {
    const url = `${BASE_URL}${path}`;
    let res: Response;
    try {
        res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: JSON.stringify(body),
            credentials: "omit",
        });
    } catch (err) {
        // fetch rejects on network failure, CORS rejection, or DNS failure.
        // Diagnostic console.error so devs can tell these apart from a 4xx.
        // Does NOT leak to the user-visible error message (mapped to
        // `frontegg_unavailable` in the caller).
        // eslint-disable-next-line no-console
        console.error(
            `[frontegg-rest] fetch to ${url} failed before reaching the server. ` +
                "Likely causes: (1) Frontegg's tenant Allowed-Origins list does not include " +
                `${typeof window !== "undefined" ? window.location.origin : "this origin"}; ` +
                "(2) NEXT_PUBLIC_FRONTEGG_BASE_URL is wrong (should be the tenant-scoped subdomain, " +
                "e.g. https://app-xxxx.frontegg.com); (3) the server is unreachable from your network. " +
                "Open the Network tab and look at the OPTIONS preflight to confirm.",
            err,
        );
        throw err;
    }
    let json: FronteggTokenResponse | Record<string, unknown> = {};
    try {
        json = await res.json();
    } catch {
        // Empty/non-JSON body — leave json as {}.
    }
    return { ok: res.ok, status: res.status, json };
}

export async function signIn(args: {
    email: string;
    password: string;
}): Promise<SignInResult> {
    if (!_assertBaseUrlConfigured()) {
        return { kind: "frontegg_unavailable" };
    }
    try {
        const r = await postFrontegg("/identity/resources/auth/v1/user", {
            email: args.email,
            password: args.password,
        });

        if (r.ok) {
            const tokens = normalizeTokens(r.json as FronteggTokenResponse);
            if (tokens) return tokens;
            // 200 but no tokens means an MFA challenge.
            const mfaToken =
                (r.json as FronteggTokenResponse).mfaToken ??
                (r.json as FronteggTokenResponse).mfa_token;
            if (mfaToken) return { kind: "mfa_required", mfaToken };
            return { kind: "unknown_error", status: r.status };
        }

        // 4xx: bad credentials / unknown email / Frontegg validation error.
        // Non-leaky: collapse 400/401/403/404 → invalid_credentials.
        if (r.status >= 400 && r.status < 500) {
            return { kind: "invalid_credentials" };
        }
        return { kind: "frontegg_unavailable" };
    } catch (_err) {
        return { kind: "frontegg_unavailable" };
    }
}

export async function signUp(args: {
    email: string;
    password: string;
    companyName: string;
    name: string;
}): Promise<SignUpResult> {
    if (!_assertBaseUrlConfigured()) {
        return { kind: "frontegg_unavailable" };
    }
    try {
        // `companyName` names the Frontegg tenant provisioned for the new
        // user; it is required (FR-018) so MCP-originated tenants are named
        // consistently with main-app sign-ups, not Frontegg-auto-generated.
        // `name` is required by Frontegg's signUp endpoint (FR-019).
        const body: Record<string, unknown> = {
            email: args.email,
            password: args.password,
            companyName: args.companyName,
            name: args.name,
        };

        const r = await postFrontegg("/identity/resources/users/v1/signUp", body);

        if (r.ok) {
            // Frontegg's signUp nests its auth payload under `authResponse`,
            // unlike signIn which returns the token fields top-level (FR-020).
            // Token presence is the completion gate — the top-level
            // `shouldActivate` flag is informational and not consulted.
            const raw = r.json as Record<string, unknown>;
            const authPayload =
                (raw.authResponse as FronteggTokenResponse | undefined) ??
                (raw as FronteggTokenResponse);
            const tokens = normalizeTokens(authPayload);
            if (tokens) return tokens;
            // No usable token in the response — Frontegg policy requires
            // email verification before issuing tokens. Common default.
            return { kind: "verification_pending" };
        }

        if (r.status === 409) {
            // Email already in use. Non-leaky message exposed at the UI layer.
            return { kind: "signup_conflict" };
        }
        if (r.status >= 400 && r.status < 500) {
            return { kind: "unknown_error", status: r.status };
        }
        return { kind: "frontegg_unavailable" };
    } catch (_err) {
        return { kind: "frontegg_unavailable" };
    }
}
