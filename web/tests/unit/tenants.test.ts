import { describe, expect, it } from "vitest";

import {
    activeTenantId,
    isMultiTenant,
    tenantOptions,
    type FronteggTenant,
} from "@/lib/tenants";

const TENANTS: FronteggTenant[] = [
    { id: "org-a", name: "Acme" },
    { id: "org-b", name: "Beta Corp" },
    { id: "org-c", name: "Gamma" },
];

describe("isMultiTenant", () => {
    it("is false for a single-org user", () => {
        expect(isMultiTenant({ tenantId: "org-a", tenantIds: ["org-a"] })).toBe(false);
    });

    it("is false when membership is missing/empty", () => {
        expect(isMultiTenant(null)).toBe(false);
        expect(isMultiTenant({})).toBe(false);
        expect(isMultiTenant({ tenantIds: [] })).toBe(false);
    });

    it("is true for a multi-org user", () => {
        expect(
            isMultiTenant({ tenantId: "org-a", tenantIds: ["org-a", "org-b"] }),
        ).toBe(true);
    });
});

describe("activeTenantId", () => {
    it("returns the active tenant id, else null", () => {
        expect(activeTenantId({ tenantId: "org-b" })).toBe("org-b");
        expect(activeTenantId({})).toBeNull();
        expect(activeTenantId(null)).toBeNull();
    });
});

describe("tenantOptions", () => {
    it("maps membership to named options with exactly one active", () => {
        const opts = tenantOptions(
            { tenantId: "org-b", tenantIds: ["org-a", "org-b"] },
            TENANTS,
        );
        expect(opts).toEqual([
            { id: "org-a", name: "Acme", isActive: false },
            { id: "org-b", name: "Beta Corp", isActive: true },
        ]);
        expect(opts.filter((o) => o.isActive)).toHaveLength(1);
    });

    it("sorts options alphabetically by name regardless of membership order (FR-005d)", () => {
        const opts = tenantOptions(
            { tenantId: "org-c", tenantIds: ["org-c", "org-a", "org-b"] },
            TENANTS, // Gamma, Acme, Beta Corp in membership order
        );
        expect(opts.map((o) => o.name)).toEqual(["Acme", "Beta Corp", "Gamma"]);
        // Active flag survives the sort.
        expect(opts.find((o) => o.isActive)?.name).toBe("Gamma");
    });

    it("excludes tenants the user is not a member of (FR-014)", () => {
        const opts = tenantOptions(
            { tenantId: "org-a", tenantIds: ["org-a"] },
            TENANTS,
        );
        expect(opts.map((o) => o.id)).toEqual(["org-a"]);
    });

    it("resolves the name when membership matches the record's tenantId (not id)", () => {
        // Frontegg records carry both `id` and `tenantId`; membership keys on
        // `tenantId`. Name must still resolve — never leak the raw id.
        const opts = tenantOptions(
            { tenantId: "tid-1", tenantIds: ["tid-1"] },
            [{ id: "record-1", tenantId: "tid-1", name: "Acme" }],
        );
        expect(opts[0]).toEqual({ id: "tid-1", name: "Acme", isActive: true });
    });

    it("shows a placeholder (never the id) when a member org has no name", () => {
        const opts = tenantOptions(
            { tenantId: "org-x", tenantIds: ["org-x"] },
            [{ id: "org-x", name: "  " }],
        );
        expect(opts[0]).toEqual({
            id: "org-x",
            name: "Unnamed organization",
            isActive: true,
        });
        // The raw id must not appear as the display name.
        expect(opts[0].name).not.toBe("org-x");
    });

    it("is empty when there is no membership", () => {
        expect(tenantOptions({}, TENANTS)).toEqual([]);
        expect(tenantOptions(null, TENANTS)).toEqual([]);
    });
});
