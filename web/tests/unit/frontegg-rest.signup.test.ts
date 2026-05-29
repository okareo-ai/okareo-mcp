import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// frontegg-rest.ts captures NEXT_PUBLIC_FRONTEGG_BASE_URL into a module-level
// constant at import time, so the env var must be stubbed and the module
// re-imported fresh before each test.
const BASE_URL = "https://app-test.frontegg.com";

async function importSignUp() {
  vi.stubEnv("NEXT_PUBLIC_FRONTEGG_BASE_URL", BASE_URL);
  vi.resetModules();
  return (await import("@/lib/frontegg-rest")).signUp;
}

describe("signUp — response token parsing (FR-020)", () => {
  let signUp: Awaited<ReturnType<typeof importSignUp>>;

  beforeEach(async () => {
    vi.stubGlobal("fetch", vi.fn());
    signUp = await importSignUp();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  const args = {
    email: "new@example.com",
    password: "hunter2hunter2",
    companyName: "Acme Inc",
    name: "Ada Lovelace",
  };

  function mockResponse(status: number, json: unknown) {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: status >= 200 && status < 300,
      status,
      json: async () => json,
    });
  }

  it("resolves a nested authResponse to kind: tokens (FR-020)", async () => {
    mockResponse(200, {
      authResponse: {
        accessToken: "header.body.sig",
        refreshToken: "rt-abc",
        expiresIn: 86400,
      },
      shouldActivate: false,
      tenantId: "t-1",
      userId: "u-1",
    });
    const r = await signUp(args);
    expect(r.kind).toBe("tokens");
    if (r.kind === "tokens") {
      expect(r.accessToken).toBe("header.body.sig");
      expect(r.refreshToken).toBe("rt-abc");
      expect(r.expiresIn).toBe(86400);
    }
  });

  it("resolves a top-level token shape to kind: tokens (fallback path)", async () => {
    mockResponse(201, {
      accessToken: "header.body.sig",
      refreshToken: "rt-top",
      expiresIn: 3600,
    });
    const r = await signUp(args);
    expect(r.kind).toBe("tokens");
  });

  it("resolves a token-less response to verification_pending", async () => {
    mockResponse(201, { shouldActivate: true, tenantId: "t-2", userId: "u-2" });
    const r = await signUp(args);
    expect(r.kind).toBe("verification_pending");
  });

  it("does not force verification_pending when shouldActivate is true but a token is present", async () => {
    mockResponse(200, {
      authResponse: {
        accessToken: "header.body.sig",
        refreshToken: "rt-xyz",
        expiresIn: 86400,
      },
      shouldActivate: true,
    });
    const r = await signUp(args);
    expect(r.kind).toBe("tokens");
  });
});
