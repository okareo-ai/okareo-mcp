import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";

// Integration-style test for FR-020: exercises the REAL signUp() client
// (only the network fetch and the handoff transport are mocked) wired
// through AuthPanel. Guards that a Frontegg signUp response whose token is
// nested under `authResponse` drives the OAuth handoff rather than
// stranding the new user on a verification warning.
vi.mock("@/lib/handoff", () => ({
  postHandoff: vi.fn(),
}));

import { AuthPanel } from "@/components/AuthPanel";
import { postHandoff } from "@/lib/handoff";

describe("AuthPanel — sign-up handoff with nested authResponse (FR-020)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "location", {
      value: {
        ...window.location,
        assign: vi.fn(),
        origin: "https://tools.okareo.com",
      },
      writable: true,
    });
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("routes a nested-authResponse sign-up success into postHandoff", async () => {
    // Realistic Frontegg signUp response: tokens nested under authResponse,
    // shouldActivate false.
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        authResponse: {
          accessToken: "header.body.sig",
          refreshToken: "rt-abc",
          expiresIn: 86400,
        },
        shouldActivate: false,
        tenantId: "t-1",
        userId: "u-1",
      }),
    });
    (postHandoff as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "success",
      redirectUrl: "https://claude.ai/cb?code=okm_xxx&state=s",
    });

    const user = userEvent.setup();
    render(
      <MantineProvider>
        <AuthPanel pendingCode="okm_test_pending_code_1234" />
      </MantineProvider>,
    );

    await user.click(screen.getByRole("tab", { name: /create account/i }));
    await user.type(screen.getByLabelText(/email/i), "new@example.com");
    await user.type(screen.getByLabelText(/password/i), "good-password-123");
    await user.type(screen.getByLabelText(/company name/i), "Acme Robotics");
    await user.type(screen.getByLabelText(/^name/i), "Ada Lovelace");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(postHandoff).toHaveBeenCalledWith(
        expect.objectContaining({
          pendingCode: "okm_test_pending_code_1234",
          fronteggAccessToken: "header.body.sig",
          fronteggRefreshToken: "rt-abc",
          fronteggExpiresIn: 86400,
        }),
      );
    });
    expect(await screen.findByText(/you're signed in/i)).toBeInTheDocument();
  });
});
