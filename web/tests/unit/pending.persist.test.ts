import { beforeEach, describe, expect, it } from "vitest";

import {
    clearPendingCode,
    parsePendingCode,
    persistPendingCode,
    resolvePendingCode,
} from "@/lib/pending";

// Survival of the one-time `pending` code across the Google full-page redirect
// (feature 029, research.md R4). resolvePendingCode prefers a fresh URL code,
// else falls back to the persisted one, else null.

const VALID = "okm_valid_code_1234";

describe("pending code persistence (feature 029)", () => {
    beforeEach(() => {
        window.sessionStorage.clear();
    });

    it("resolves a valid ?pending= from the URL and persists it for the round-trip", () => {
        expect(resolvePendingCode(`?pending=${VALID}`)).toBe(VALID);
        // After the redirect the URL no longer carries ?pending=, but it survives:
        expect(resolvePendingCode("")).toBe(VALID);
    });

    it("restores a previously persisted code when the URL has no ?pending=", () => {
        persistPendingCode(VALID);
        expect(resolvePendingCode("")).toBe(VALID);
    });

    it("returns null when neither the URL nor storage has a code", () => {
        expect(resolvePendingCode("")).toBeNull();
    });

    it("clears the persisted code", () => {
        persistPendingCode(VALID);
        clearPendingCode();
        expect(resolvePendingCode("")).toBeNull();
    });

    it("defensively rejects a malformed persisted value", () => {
        window.sessionStorage.setItem("okareo.mcp.pending", "not-a-valid-code");
        expect(resolvePendingCode("")).toBeNull();
    });

    it("prefers a fresh valid URL code over a stale stored one", () => {
        persistPendingCode("okm_old_code_9999");
        expect(resolvePendingCode(`?pending=${VALID}`)).toBe(VALID);
        expect(resolvePendingCode("")).toBe(VALID);
    });

    it("still classifies absent and malformed via parsePendingCode", () => {
        expect(parsePendingCode("").kind).toBe("absent");
        expect(parsePendingCode("?pending=x").kind).toBe("malformed");
    });
});
