import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";

// US3 sign-up: a completed sign-up that yields an authenticated session hands
// off normally; a sign-up that still requires activation/verification leaves the
// user unauthenticated, so the page must NOT silently hand off — the embedded
// box keeps showing its verify-email screen (loginWithRedirect path).

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

function renderPage() {
    return render(
        <MantineProvider>
            <LoginPage />
        </MantineProvider>,
    );
}

describe("sign-up hand-off (US3)", () => {
    beforeEach(() => {
        window.sessionStorage.clear();
        h.auth = { isAuthenticated: false, isLoading: false, user: null };
        h.loginWithRedirect.mockReset();
        h.postHandoff.mockReset();
        setSearch(`?pending=${VALID}`);
    });

    it("hands off when sign-up completes with an authenticated session", async () => {
        h.auth = {
            isAuthenticated: true,
            isLoading: false,
            user: {
                accessToken: "su.jwt.sig",
                refreshToken: "rt-su",
                expiresIn: 3600,
                email: "new@company.com",
            },
        };
        h.postHandoff.mockResolvedValue({
            kind: "success",
            redirectUrl: "https://claude.ai/cb?code=okm_su",
        });

        renderPage();

        await waitFor(() => expect(h.postHandoff).toHaveBeenCalledTimes(1));
    });

    it("does NOT hand off when sign-up requires activation (still unauthenticated)", async () => {
        h.auth = { isAuthenticated: false, isLoading: false, user: null };

        renderPage();

        await waitFor(() => expect(h.loginWithRedirect).toHaveBeenCalled());
        expect(h.postHandoff).not.toHaveBeenCalled();
    });
});
