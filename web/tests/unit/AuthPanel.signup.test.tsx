import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";

import { AuthPanel } from "@/components/AuthPanel";
import { signUp } from "@/lib/frontegg-rest";
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

async function switchToSignUp(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("tab", { name: /create account/i }));
}

describe("AuthPanel — sign-up required fields (FR-018 / FR-019)", () => {
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
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders required Company name and Name fields on the sign-up tab", async () => {
    const user = userEvent.setup();
    renderPanel();
    await switchToSignUp(user);
    expect(screen.getByLabelText(/company name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^name/i)).toBeInTheDocument();
  });

  it("renders neither Company name nor Name on the sign-in tab", () => {
    renderPanel();
    expect(screen.queryByLabelText(/company name/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^name/i)).not.toBeInTheDocument();
  });

  it("blocks submission and makes no Frontegg call when company name is empty (FR-018)", async () => {
    const user = userEvent.setup();
    renderPanel();
    await switchToSignUp(user);

    // Everything else valid; only the company name is missing.
    await user.type(screen.getByLabelText(/email/i), "new@example.com");
    await user.type(screen.getByLabelText(/password/i), "good-password-123");
    await user.type(screen.getByLabelText(/^name/i), "Ada Lovelace");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByText(/company name is required/i)).toBeInTheDocument();
    expect(signUp).not.toHaveBeenCalled();
  });

  it("blocks submission and makes no Frontegg call when name is empty (FR-019)", async () => {
    const user = userEvent.setup();
    renderPanel();
    await switchToSignUp(user);

    // Everything else valid; only the name is missing.
    await user.type(screen.getByLabelText(/email/i), "new@example.com");
    await user.type(screen.getByLabelText(/password/i), "good-password-123");
    await user.type(screen.getByLabelText(/company name/i), "Acme Robotics");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByText(/^name is required/i)).toBeInTheDocument();
    expect(signUp).not.toHaveBeenCalled();
  });

  it("treats whitespace-only company name and name as empty", async () => {
    const user = userEvent.setup();
    renderPanel();
    await switchToSignUp(user);

    await user.type(screen.getByLabelText(/email/i), "new@example.com");
    await user.type(screen.getByLabelText(/password/i), "good-password-123");
    await user.type(screen.getByLabelText(/company name/i), "   ");
    await user.type(screen.getByLabelText(/^name/i), "   ");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    expect(await screen.findByText(/company name is required/i)).toBeInTheDocument();
    expect(screen.getByText(/^name is required/i)).toBeInTheDocument();
    expect(signUp).not.toHaveBeenCalled();
  });

  it("passes the entered company name and name to signUp", async () => {
    (signUp as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      kind: "verification_pending",
    });
    const user = userEvent.setup();
    renderPanel();
    await switchToSignUp(user);

    await user.type(screen.getByLabelText(/email/i), "new@example.com");
    await user.type(screen.getByLabelText(/password/i), "good-password-123");
    await user.type(screen.getByLabelText(/company name/i), "Acme Robotics");
    await user.type(screen.getByLabelText(/^name/i), "Ada Lovelace");
    await user.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(signUp).toHaveBeenCalledWith(
        expect.objectContaining({
          email: "new@example.com",
          password: "good-password-123",
          companyName: "Acme Robotics",
          name: "Ada Lovelace",
        }),
      );
    });
    expect(postHandoff).not.toHaveBeenCalled();
  });
});
