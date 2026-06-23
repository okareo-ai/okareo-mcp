// Map the @frontegg/react auth state into a HandoffRequest (feature 029).
//
// After the embedded box authenticates the user (email/password OR Google), the
// page reads the tokens from useAuth().user and posts them to /oauth/handoff —
// the SAME request the hand-built forms used to build from a REST response. Only
// the source changes; the wire shape is unchanged (contracts/oauth-handoff.md).

import type { HandoffRequest } from "@/lib/handoff";

// Subset of @frontegg/react's User we depend on. accessToken/expiresIn are
// always present once authenticated; refreshToken is typed optional by the SDK
// and required by the server, so we treat its absence as an error (below).
export type FronteggUserTokens = {
    accessToken?: string;
    refreshToken?: string;
    expiresIn?: number;
};

export type TokenExtraction =
    | { ok: true; request: HandoffRequest }
    | { ok: false };

/**
 * Build a HandoffRequest from the authenticated Frontegg user. Returns
 * { ok: false } when any required token field is missing, so the caller can
 * surface a neutral error instead of posting an invalid hand-off.
 */
export function toHandoffRequest(
    pendingCode: string,
    user: FronteggUserTokens | null | undefined,
): TokenExtraction {
    const accessToken = user?.accessToken;
    const refreshToken = user?.refreshToken;
    const expiresIn = user?.expiresIn;

    if (
        !accessToken ||
        !refreshToken ||
        typeof expiresIn !== "number" ||
        expiresIn <= 0
    ) {
        return { ok: false };
    }

    return {
        ok: true,
        request: {
            pendingCode,
            fronteggAccessToken: accessToken,
            fronteggRefreshToken: refreshToken,
            fronteggExpiresIn: expiresIn,
        },
    };
}
