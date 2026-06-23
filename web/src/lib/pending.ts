// Parse and validate the `pending` query param the OAuth Proxy passes when
// it redirects the browser to /login. The value is the opaque
// PendingAuthorization code minted by OAuthStateStore.create_pending
// server-side; the page threads it back through /oauth/handoff on submit.
//
// Validation matches the server's contract (specs/021-embedded-login/contracts/handoff-endpoint.openapi.yaml
// `pending_code` schema: ^okm_[A-Za-z0-9_-]+$, length 8-128).

const PENDING_CODE_PATTERN = /^okm_[A-Za-z0-9_-]+$/;
const PENDING_CODE_MIN_LEN = 8;
const PENDING_CODE_MAX_LEN = 128;

export type PendingCodeStatus =
    | { kind: "valid"; code: string }
    | { kind: "absent" }
    | { kind: "malformed" };

/**
 * Read `pending` from the URL search string. Returns a discriminated union so
 * callers can distinguish a legitimate "no flow in progress" landing (absent)
 * from an attack/typo (malformed) and render the appropriate UI.
 *
 * Intended to be called inside a client component that has access to `window`.
 */
export function parsePendingCode(searchString?: string): PendingCodeStatus {
    const search =
        searchString ??
        (typeof window !== "undefined" ? window.location.search : "");

    if (!search) {
        return { kind: "absent" };
    }

    const params = new URLSearchParams(search);
    const raw = params.get("pending");
    if (raw === null) {
        return { kind: "absent" };
    }

    if (raw.length < PENDING_CODE_MIN_LEN || raw.length > PENDING_CODE_MAX_LEN) {
        return { kind: "malformed" };
    }
    if (!PENDING_CODE_PATTERN.test(raw)) {
        return { kind: "malformed" };
    }

    return { kind: "valid", code: raw };
}

// --- Survival across the social-login redirect (feature 029, research.md R4) ---
//
// Google sign-in does a full-page redirect (browser leaves /login, returns to
// /login/oauth/callback), so the `?pending=` query param is gone when the SDK
// reports authenticated. We stash the validated code in sessionStorage on first
// load and read it back afterwards. sessionStorage (tab-scoped) suits a
// short-lived, single-use code better than localStorage.

const PENDING_STORAGE_KEY = "okareo.mcp.pending";

function safeSessionStorage(): Storage | null {
    try {
        return typeof window !== "undefined" ? window.sessionStorage : null;
    } catch {
        // Access can throw in some privacy modes / sandboxed iframes.
        return null;
    }
}

/** Persist a validated pending code so it survives the social redirect. */
export function persistPendingCode(code: string): void {
    safeSessionStorage()?.setItem(PENDING_STORAGE_KEY, code);
}

/**
 * Resolve the pending code for this flow: prefer a fresh, valid `?pending=` in
 * the URL (persisting it); otherwise fall back to a previously persisted code
 * (the post-redirect case). Returns null when neither yields a valid code.
 */
export function resolvePendingCode(searchString?: string): string | null {
    const fromUrl = parsePendingCode(searchString);
    if (fromUrl.kind === "valid") {
        persistPendingCode(fromUrl.code);
        return fromUrl.code;
    }
    const stored = safeSessionStorage()?.getItem(PENDING_STORAGE_KEY) ?? null;
    if (stored === null) return null;
    // Re-validate stored values defensively before trusting them.
    const reparsed = parsePendingCode(`?pending=${stored}`);
    return reparsed.kind === "valid" ? reparsed.code : null;
}

/** Clear the persisted code once the hand-off has consumed it. */
export function clearPendingCode(): void {
    safeSessionStorage()?.removeItem(PENDING_STORAGE_KEY);
}
