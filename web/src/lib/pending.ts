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
