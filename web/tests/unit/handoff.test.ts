import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { postHandoff } from "@/lib/handoff";

const ORIGIN = "https://tools.okareo.com";

describe("postHandoff", () => {
  beforeEach(() => {
    Object.defineProperty(window, "location", {
      value: { ...window.location, origin: ORIGIN, assign: vi.fn() },
      writable: true,
    });
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const req = {
    pendingCode: "okm_test_pending",
    fronteggAccessToken: "header.body.sig",
    fronteggRefreshToken: "rt-123",
    fronteggExpiresIn: 3600,
  };

  it("returns success with redirect_url on 200", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 200,
      json: async () => ({
        redirect_url: "https://claude.ai/cb?code=okm_xxx&state=s",
      }),
    });
    const r = await postHandoff(req);
    expect(r.kind).toBe("success");
    if (r.kind === "success") {
      expect(r.redirectUrl).toBe("https://claude.ai/cb?code=okm_xxx&state=s");
    }
  });

  it("posts to <origin>/oauth/handoff with the contract body shape", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 200,
      json: async () => ({ redirect_url: "https://x/cb?code=c" }),
    });
    await postHandoff(req);
    expect(fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledInit] = (
      fetch as unknown as ReturnType<typeof vi.fn>
    ).mock.calls[0];
    expect(calledUrl).toBe(`${ORIGIN}/oauth/handoff`);
    expect(calledInit.method).toBe("POST");
    expect(calledInit.headers).toEqual({ "Content-Type": "application/json" });
    const body = JSON.parse(calledInit.body);
    expect(body).toEqual({
      pending_code: "okm_test_pending",
      frontegg_access_token: "header.body.sig",
      frontegg_refresh_token: "rt-123",
      frontegg_expires_in: 3600,
    });
  });

  it("maps 400 invalid_grant → expired", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 400,
      json: async () => ({
        error: "invalid_grant",
        error_description: "expired",
      }),
    });
    const r = await postHandoff(req);
    expect(r.kind).toBe("expired");
  });

  it("maps 400 invalid_token → invalid_token", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 400,
      json: async () => ({ error: "invalid_token" }),
    });
    const r = await postHandoff(req);
    expect(r.kind).toBe("invalid_token");
  });

  it("maps 400 invalid_request → invalid_request", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 400,
      json: async () => ({ error: "invalid_request" }),
    });
    const r = await postHandoff(req);
    expect(r.kind).toBe("invalid_request");
  });

  it("maps 403 → forbidden", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 403,
      json: async () => ({ error: "forbidden" }),
    });
    const r = await postHandoff(req);
    expect(r.kind).toBe("forbidden");
  });

  it("maps 503 → frontegg_unavailable", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 503,
      json: async () => ({ error: "temporarily_unavailable" }),
    });
    const r = await postHandoff(req);
    expect(r.kind).toBe("frontegg_unavailable");
  });

  it("maps fetch rejection (network error) → frontegg_unavailable", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("connection refused"),
    );
    const r = await postHandoff(req);
    expect(r.kind).toBe("frontegg_unavailable");
  });

  it("includes credentials: 'same-origin' for cookie scoping", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: 200,
      json: async () => ({ redirect_url: "https://x/cb?code=c" }),
    });
    await postHandoff(req);
    const [, calledInit] = (fetch as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0];
    expect(calledInit.credentials).toBe("same-origin");
  });
});
