import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";

// Feature 030 sign-in selection flow: single-tenant passthrough (US2),
// multi-tenant select+authorize (US1), and re-selection via switchTenant (US3).

const h = vi.hoisted(() => ({
    auth: {
        isAuthenticated: false,
        isLoading: false,
        user: null as Record<string, unknown> | null,
    },
    loginWithRedirect: vi.fn(),
    switchTenant: vi.fn(),
    tenants: [] as Array<{ id: string; name: string }>,
    postHandoff: vi.fn(),
}));

vi.mock("@frontegg/react", () => ({
    useAuth: () => h.auth,
    useLoginWithRedirect: () => h.loginWithRedirect,
    useAuthActions: () => ({ switchTenant: h.switchTenant }),
    useTenantsState: () => ({ tenants: h.tenants }),
}));

vi.mock("@/lib/handoff", async (importOriginal) => {
    const actual = await importOriginal<typeof import("@/lib/handoff")>();
    return { ...actual, postHandoff: h.postHandoff };
});

import LoginPage from "@/app/page";

const VALID = "okm_valid_code_1234";
const TOKENS = {
    accessToken: "a".repeat(40),
    refreshToken: "r".repeat(40),
    expiresIn: 3600,
};

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

describe("feature 030 sign-in selection flow", () => {
    beforeEach(() => {
        window.sessionStorage.clear();
        h.auth = { isAuthenticated: false, isLoading: false, user: null };
        h.loginWithRedirect.mockReset();
        h.switchTenant.mockReset().mockResolvedValue(undefined);
        h.postHandoff.mockReset().mockResolvedValue({
            kind: "success",
            redirectUrl: "https://client.example/cb?code=okm_valid_code_1234",
        });
        h.tenants = [
            { id: "org-a", name: "Acme" },
            { id: "org-b", name: "Beta Corp" },
        ];
        setSearch(`?pending=${VALID}`);
    });

    it("US2: single-tenant user hands off directly with no selected_tenant_id", async () => {
        h.auth = {
            isAuthenticated: true,
            isLoading: false,
            user: { ...TOKENS, tenantId: "org-a", tenantIds: ["org-a"] },
        };
        renderPage();
        await waitFor(() => expect(h.postHandoff).toHaveBeenCalledTimes(1));
        expect(h.postHandoff.mock.calls[0][0]).not.toHaveProperty("selectedTenantId");
        // No selection screen was shown.
        expect(screen.queryByText("Choose an organization")).not.toBeInTheDocument();
    });

    it("US1: multi-tenant user sees the selection screen and authorizes the active org", async () => {
        h.auth = {
            isAuthenticated: true,
            isLoading: false,
            user: { ...TOKENS, tenantId: "org-a", tenantIds: ["org-a", "org-b"] },
        };
        renderPage();

        // Selection screen appears; no premature hand-off.
        await screen.findByText("Choose an organization");
        expect(h.postHandoff).not.toHaveBeenCalled();
        expect(screen.getByDisplayValue("Acme")).toBeInTheDocument();

        await userEvent.click(screen.getByRole("button", { name: /^Authorize$/ }));
        await waitFor(() => expect(h.postHandoff).toHaveBeenCalledTimes(1));
        expect(h.postHandoff.mock.calls[0][0]).toMatchObject({
            selectedTenantId: "org-a",
        });
    });

    it("US3: choosing a different org calls switchTenant with that id", async () => {
        h.auth = {
            isAuthenticated: true,
            isLoading: false,
            user: { ...TOKENS, tenantId: "org-a", tenantIds: ["org-a", "org-b"] },
        };
        renderPage();

        await screen.findByText("Choose an organization");
        await userEvent.click(
            screen.getByRole("textbox", { name: /Organization to authorize/i }),
        );
        await userEvent.click(screen.getByRole("option", { name: "Beta Corp" }));
        expect(h.switchTenant).toHaveBeenCalledWith({
            tenantId: "org-b",
            silentReload: false,
        });
        // No hand-off happens on a re-select — only on Authorize.
        expect(h.postHandoff).not.toHaveBeenCalled();
    });
});
