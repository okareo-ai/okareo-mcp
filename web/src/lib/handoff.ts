// Client for POST /oauth/handoff — the server-side endpoint defined in
// specs/021-embedded-login/contracts/handoff-endpoint.openapi.yaml.
//
// On success the server returns the URL the page should navigate to (the
// MCP-client's registered redirect_uri with our minted code). On failure
// the response shape is the standard {error, error_description} envelope.

export type HandoffSuccess = {
    kind: "success";
    redirectUrl: string;
};

export type HandoffError =
    | { kind: "expired"; message: string }            // 400 invalid_grant — pending unknown/expired
    | { kind: "invalid_token"; message: string }      // 400 invalid_token — JWT validation failed
    | { kind: "invalid_request"; message: string }    // 400 invalid_request — bad shape
    | { kind: "tenant_mismatch"; message: string }    // 400 tenant_mismatch — org ≠ returned credentials
    | { kind: "forbidden"; message: string }          // 403 — CSRF / origin check
    | { kind: "frontegg_unavailable"; message: string }; // 503 / network

export type HandoffResult = HandoffSuccess | HandoffError;

export type HandoffRequest = {
    pendingCode: string;
    fronteggAccessToken: string;
    fronteggRefreshToken: string;
    fronteggExpiresIn: number;
    // The organization the user authorized (feature 030). When present the
    // server verifies it matches the token's tenant claim and rejects a
    // mismatch. Omitted by the single-tenant path.
    selectedTenantId?: string;
};

function mcpBaseUrl(): string {
    // Production: same-origin (page is served from MCP origin).
    // Dev: NEXT_PUBLIC_MCP_BASE_URL points at the Python server (e.g., :8080).
    const fromEnv =
        typeof process !== "undefined"
            ? process.env.NEXT_PUBLIC_MCP_BASE_URL
            : undefined;
    if (fromEnv) return fromEnv.replace(/\/$/, "");
    if (typeof window !== "undefined") {
        return window.location.origin.replace(/\/$/, "");
    }
    return "";
}

export async function postHandoff(req: HandoffRequest): Promise<HandoffResult> {
    const url = `${mcpBaseUrl()}/oauth/handoff`;
    let response: Response;
    try {
        response = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({
                pending_code: req.pendingCode,
                frontegg_access_token: req.fronteggAccessToken,
                frontegg_refresh_token: req.fronteggRefreshToken,
                frontegg_expires_in: req.fronteggExpiresIn,
                ...(req.selectedTenantId
                    ? { selected_tenant_id: req.selectedTenantId }
                    : {}),
            }),
        });
    } catch (_err) {
        return {
            kind: "frontegg_unavailable",
            message: "Could not reach the Okareo MCP server. Please retry from your copilot.",
        };
    }

    let payload: { redirect_url?: string; error?: string; error_description?: string } = {};
    try {
        payload = await response.json();
    } catch {
        // Empty / non-JSON body.
    }

    if (response.status === 200 && typeof payload.redirect_url === "string") {
        return { kind: "success", redirectUrl: payload.redirect_url };
    }

    if (response.status === 403) {
        return {
            kind: "forbidden",
            message: "This sign-in flow expired or originated from an unexpected page. Please retry from your copilot.",
        };
    }

    if (response.status === 400) {
        switch (payload.error) {
            case "invalid_grant":
                return {
                    kind: "expired",
                    message: "This sign-in session has expired. Please retry from your copilot.",
                };
            case "invalid_token":
                return {
                    kind: "invalid_token",
                    message: "Authentication succeeded but the server couldn't validate the returned credentials. Please retry from your copilot.",
                };
            case "tenant_mismatch":
                return {
                    kind: "tenant_mismatch",
                    message: "The organization you selected didn't match the credentials returned. Please choose the organization again.",
                };
            case "invalid_request":
                return {
                    kind: "invalid_request",
                    message: payload.error_description ?? "Sign-in request was malformed.",
                };
        }
    }

    if (response.status === 503) {
        return {
            kind: "frontegg_unavailable",
            message: "The authentication service is temporarily unavailable. Please retry in a moment.",
        };
    }

    return {
        kind: "frontegg_unavailable",
        message: "Sign-in failed for an unexpected reason. Please retry from your copilot.",
    };
}
