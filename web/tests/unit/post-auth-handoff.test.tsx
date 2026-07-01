import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";

// US1 orchestration: once the embedded box authenticates the user, the page
// posts the Frontegg tokens to /oauth/handoff exactly once and navigates back.

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

describe("post-auth hand-off orchestration (US1)", () => {
    beforeEach(() => {
        window.sessionStorage.clear();
        h.auth = { isAuthenticated: false, isLoading: false, user: null };
        h.loginWithRedirect.mockReset();
        h.postHandoff.mockReset();
        setSearch("");
    });

    it("posts the hand-off once and navigates on success when authenticated with a pending code", async () => {
        setSearch(`?pending=${VALID}`);
        h.auth = {
            isAuthenticated: true,
            isLoading: false,
            user: {
                accessToken: "h.b.s",
                refreshToken: "rt-1",
                expiresIn: 3600,
                email: "u@x.com",
            },
        };
        h.postHandoff.mockResolvedValue({
            kind: "success",
            redirectUrl: "https://claude.ai/cb?code=okm_x",
        });

        renderPage();

        await waitFor(() => expect(h.postHandoff).toHaveBeenCalledTimes(1));
        expect(h.postHandoff).toHaveBeenCalledWith({
            pendingCode: VALID,
            fronteggAccessToken: "h.b.s",
            fronteggRefreshToken: "rt-1",
            fronteggExpiresIn: 3600,
        });
        await waitFor(() =>
            expect(window.location.assign).toHaveBeenCalledWith(
                "https://claude.ai/cb?code=okm_x",
            ),
        );
    });

    it("shows the box and does NOT hand off when unauthenticated", async () => {
        setSearch(`?pending=${VALID}`);
        renderPage();

        await waitFor(() => expect(h.loginWithRedirect).toHaveBeenCalledTimes(1));
        expect(h.postHandoff).not.toHaveBeenCalled();
    });

    it("does nothing without a pending code, even if already authenticated", async () => {
        h.auth = {
            isAuthenticated: true,
            isLoading: false,
            user: { accessToken: "a", refreshToken: "r", expiresIn: 10 },
        };
        renderPage();

        await waitFor(() => expect(h.postHandoff).not.toHaveBeenCalled());
        expect(h.loginWithRedirect).not.toHaveBeenCalled();
    });

    it("surfaces a neutral error (no navigation) when required tokens are missing", async () => {
        setSearch(`?pending=${VALID}`);
        h.auth = {
            isAuthenticated: true,
            isLoading: false,
            user: { accessToken: "a", expiresIn: 10 }, // no refreshToken
        };
        renderPage();

        await waitFor(() => expect(h.postHandoff).not.toHaveBeenCalled());
        expect(window.location.assign).not.toHaveBeenCalled();
    });
});
