import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";

// US2 no-regression: the hand-off is method-agnostic. An email/password
// authentication (no social marker) drives the SAME single hand-off as Google,
// so the existing email/password path is preserved (SC-004).

const h = vi.hoisted(() => ({
    auth: {
        isAuthenticated: false,
        isLoading: false,
        user: null as Record<string, unknown> | null,
    },
    loginWithRedirect: vi.fn(),
    postHandoff: vi.fn(),
}));

vi.mock("@frontegg/react", () => ({
    useAuth: () => h.auth,
    useLoginWithRedirect: () => h.loginWithRedirect,
    useAuthActions: () => ({ switchTenant: vi.fn() }),
    useTenantsState: () => ({ tenants: [] }),
}));

vi.mock("@/lib/handoff", async (importOriginal) => {
    const actual = await importOriginal<typeof import("@/lib/handoff")>();
    return { ...actual, postHandoff: h.postHandoff };
});

import LoginPage from "@/app/page";

const VALID = "okm_valid_code_1234";

function setSearch(search: string) {
    Object.defineProperty(window, "location", {
        value: { ...window.location, search, assign: vi.fn() },
        writable: true,
    });
}

describe("email/password hand-off (US2 no-regression)", () => {
    beforeEach(() => {
        window.sessionStorage.clear();
        h.auth = { isAuthenticated: false, isLoading: false, user: null };
        h.loginWithRedirect.mockReset();
        h.postHandoff.mockReset();
        setSearch(`?pending=${VALID}`);
    });

    it("hands off for an email/password user the same way as any other method", async () => {
        h.auth = {
            isAuthenticated: true,
            isLoading: false,
            // A plain email/password user: no social/provider fields.
            user: {
                accessToken: "ep.jwt.sig",
                refreshToken: "rt-ep",
                expiresIn: 1800,
                email: "person@company.com",
            },
        };
        h.postHandoff.mockResolvedValue({
            kind: "success",
            redirectUrl: "https://claude.ai/cb?code=okm_ep",
        });

        render(
            <MantineProvider>
                <LoginPage />
            </MantineProvider>,
        );

        await waitFor(() => expect(h.postHandoff).toHaveBeenCalledTimes(1));
        expect(h.postHandoff).toHaveBeenCalledWith({
            pendingCode: VALID,
            fronteggAccessToken: "ep.jwt.sig",
            fronteggRefreshToken: "rt-ep",
            fronteggExpiresIn: 1800,
        });
    });
});
