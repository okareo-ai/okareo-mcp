import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";

import { AuthPanel } from "@/components/AuthPanel";
import { signIn } from "@/lib/frontegg-rest";
import { postHandoff } from "@/lib/handoff";

vi.mock("@/lib/frontegg-rest", () => ({
  signIn: vi.fn(),
  signUp: vi.fn(),
}));

vi.mock("@/lib/handoff", () => ({
  postHandoff: vi.fn(),
}));

function renderPanel(props: Partial<Parameters<typeof AuthPanel>[0]> = {}) {
  return render(
    <MantineProvider>
      <AuthPanel pendingCode="okm_test_pending_code_1234" {...props} />
    </MantineProvider>,
  );
}

describe("AuthPanel — sign-in flow (US1)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset window.location.assign mock fresh each test.
    Object.defineProperty(window, "location", {
      value: {
        ...window.location,
        assign: vi.fn(),
        origin: "https://tools.okareo.com",
      },
      writable: true,
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders email + password inputs with proper labels", () => {
    renderPanel();
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
  });

  it("renders the Sign in submit button", () => {
    renderPanel();
    expect(
      screen.getByRole("button", { name: /^sign in$/i }),
    ).toBeInTheDocument();
  });

  it("validates email format before submit", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/email/i), "not-an-email");
    await user.type(screen.getByLabelText(/password/i), "longenoughpw");
    await user.click(screen.getByRole("button", { name: /^sign in$/i }));
    expect(signIn).not.toHaveBeenCalled();
    expect(await screen.findByText(/valid email/i)).toBeInTheDocument();
  });

  it("validates password length before submit", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/email/i), "a@b.co");
    await user.type(screen.getByLabelText(/password/i), "short");
    await user.click(screen.getByRole("button", { name: /^sign in$/i }));
    expect(signIn).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/at least 8 characters/i),
    ).toBeInTheDocument();
  });

  it("on Frontegg 200 + handoff success, navigates to the returned redirect_url", async () => {
    (signIn as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "tokens",
      accessToken: "header.body.sig",
      refreshToken: "rt-1",
      expiresIn: 3600,
    });
    (postHandoff as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "success",
      redirectUrl: "https://claude.ai/cb?code=okm_x&state=s",
    });
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/email/i), "user@example.com");
    await user.type(
      screen.getByLabelText(/password/i),
      "correct-horse-battery",
    );
    await user.click(screen.getByRole("button", { name: /^sign in$/i }));

    await waitFor(() => {
      expect(signIn).toHaveBeenCalledWith({
        email: "user@example.com",
        password: "correct-horse-battery",
      });
    });
    await waitFor(() => {
      expect(postHandoff).toHaveBeenCalledWith({
        pendingCode: "okm_test_pending_code_1234",
        fronteggAccessToken: "header.body.sig",
        fronteggRefreshToken: "rt-1",
        fronteggExpiresIn: 3600,
      });
    });
    await waitFor(() => {
      expect(window.location.assign).toHaveBeenCalledWith(
        "https://claude.ai/cb?code=okm_x&state=s",
      );
    });
  });

  it("renders a success view after handoff (visible to custom-URI-scheme clients like Cursor)", async () => {
    (signIn as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "tokens",
      accessToken: "h.b.s",
      refreshToken: "rt",
      expiresIn: 3600,
    });
    (postHandoff as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "success",
      redirectUrl: "cursor://anysphere.cursor-mcp/oauth/callback?code=okm_x",
    });
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/email/i), "user@example.com");
    await user.type(screen.getByLabelText(/password/i), "good-password-123");
    await user.click(screen.getByRole("button", { name: /^sign in$/i }));

    // The success view should appear as soon as handoff returns success,
    // BEFORE the deferred window.location.assign fires. This is what
    // makes Cursor (custom URI scheme) show a non-confusing UI: the
    // browser hands off to the OS-level handler but stays on this tab.
    const status = await screen.findByRole("status");
    expect(status.textContent).toMatch(/signed in/i);
    expect(status.textContent).toMatch(/return to your copilot/i);
    // The form should no longer be visible.
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();
  });

  it("surfaces invalid_credentials in the error banner (non-leaky)", async () => {
    (signIn as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "invalid_credentials",
    });
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/email/i), "user@example.com");
    await user.type(screen.getByLabelText(/password/i), "wrong-password-1");
    await user.click(screen.getByRole("button", { name: /^sign in$/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/couldn't sign you in/i);
    // Non-leaky: must NOT confirm whether the email exists.
    expect(alert.textContent).not.toMatch(
      /no account|user not found|account does not exist/i,
    );
    // Handoff must not have been attempted.
    expect(postHandoff).not.toHaveBeenCalled();
  });

  it("surfaces frontegg_unavailable on Frontegg outage", async () => {
    (signIn as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "frontegg_unavailable",
    });
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/email/i), "user@example.com");
    await user.type(screen.getByLabelText(/password/i), "good-password-123");
    await user.click(screen.getByRole("button", { name: /^sign in$/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/temporarily unavailable/i);
  });

  it("surfaces handoff expired (sign-in succeeded but pending expired)", async () => {
    (signIn as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "tokens",
      accessToken: "h.b.s",
      refreshToken: "rt",
      expiresIn: 3600,
    });
    (postHandoff as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "expired",
      message: "expired",
    });
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/email/i), "user@example.com");
    await user.type(screen.getByLabelText(/password/i), "good-password-123");
    await user.click(screen.getByRole("button", { name: /^sign in$/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/sign-in session has expired/i);
    // We do NOT navigate on a failed handoff.
    expect(window.location.assign).not.toHaveBeenCalled();
  });

  it("disables submit while a request is in flight (prevents double-submit)", async () => {
    let resolveSignIn: (v: unknown) => void = () => {};
    (signIn as ReturnType<typeof vi.fn>).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveSignIn = resolve;
      }),
    );
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByLabelText(/email/i), "user@example.com");
    await user.type(screen.getByLabelText(/password/i), "good-password-123");
    const button = screen.getByRole("button", { name: /^sign in$/i });
    await user.click(button);

    await waitFor(() => {
      expect(button).toHaveAttribute("data-loading", "true");
    });

    // Cleanup: resolve the pending signIn so the test doesn't leak.
    resolveSignIn({ kind: "invalid_credentials" });
  });
});
