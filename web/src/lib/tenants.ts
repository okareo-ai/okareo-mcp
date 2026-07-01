// Tenant-membership helpers for the sign-in selection step (feature 030).
//
// The @frontegg/react SDK already knows every organization the authenticated
// user belongs to (`user.tenantIds`), which one is active (`user.tenantId`),
// and the named tenant records (`useTenantsState().tenants`). This module
// projects those into the minimal view-model the selection UI needs so the
// page and component stay free of SDK-shape details. Pure functions only —
// no hooks here, so they are trivially unit-testable.

// Subset of @frontegg/react's authenticated User we depend on. The SDK types
// these as always-present once authenticated; we treat them defensively.
export type FronteggTenantUser = {
    tenantId?: string;
    tenantIds?: string[];
};

// Subset of @frontegg/rest-api's ITenantsResponse we render. Frontegg carries
// both `id` and `tenantId` on a tenant record; the user's membership
// (`user.tenantIds` / `user.tenantId`) is keyed by `tenantId`, so we match on
// either to resolve the Account name reliably.
export type FronteggTenant = {
    id: string;
    tenantId?: string;
    name: string;
};

// Shown in place of a missing Account name — never the raw tenant id (FR-009).
const UNNAMED_ORG = "Unnamed organization";

// A choosable organization, projected for display (data-model.md → TenantOption).
export type TenantOption = {
    id: string;
    name: string;
    isActive: boolean;
};

/** True when the user belongs to more than one organization (FR-001/003). */
export function isMultiTenant(user: FronteggTenantUser | null | undefined): boolean {
    return (user?.tenantIds?.length ?? 0) > 1;
}

/** The organization currently slated to be authorized (the active tenant). */
export function activeTenantId(
    user: FronteggTenantUser | null | undefined,
): string | null {
    return user?.tenantId ?? null;
}

/**
 * Build the list of organizations the user may authorize, by Account name,
 * flagging the active one. Filters the named tenant records down to the user's
 * membership so an org the user no longer belongs to is never renderable
 * (FR-009/FR-014). When a name can't be resolved, shows a neutral placeholder
 * — never the raw tenant id (FR-009).
 */
export function tenantOptions(
    user: FronteggTenantUser | null | undefined,
    tenants: readonly FronteggTenant[] | null | undefined,
): TenantOption[] {
    const memberIds = user?.tenantIds ?? [];
    const active = activeTenantId(user);
    // Index by both `id` and `tenantId`: membership ids match `tenantId`, but
    // some records key their identity on `id` — accept either.
    const byKey = new Map<string, FronteggTenant>();
    for (const t of tenants ?? []) {
        if (t.id) byKey.set(t.id, t);
        if (t.tenantId) byKey.set(t.tenantId, t);
    }
    return memberIds
        .map((id) => {
            const record = byKey.get(id);
            return {
                id,
                name: record?.name?.trim() ? record.name : UNNAMED_ORG,
                isActive: id === active,
            };
        })
        // Alphabetical by Account name (case-insensitive) so a long list is
        // predictable to scan (FR-005d).
        .sort((a, b) =>
            a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
        );
}
