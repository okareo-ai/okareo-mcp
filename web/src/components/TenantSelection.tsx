"use client";

// Organization-selection screen for multi-tenant sign-in (feature 030).
//
// A single combined screen (no separate "view current" and "select a
// different one" steps): a searchable account picker pre-set to the current
// organization plus an Authorize action. Picking a different account fires
// `onSelect(id)` immediately (the page re-scopes via the SDK); Authorize hands
// off the current account. The picker (a searchable Select) scales to users
// associated with many (20+) organizations, and its options arrive already
// sorted alphabetically by name (see lib/tenants.tenantOptions). Names only —
// the raw tenant id is never shown (FR-009).

import { Box, Button, Select, Stack, Text, Title } from "@mantine/core";

import type { TenantOption } from "@/lib/tenants";

export type TenantSelectionProps = {
    options: TenantOption[];
    onAuthorize: () => void;
    onSelect: (tenantId: string) => void;
    // Disables the controls while a switch/hand-off is in flight.
    busy?: boolean;
};

export function TenantSelection({
    options,
    onAuthorize,
    onSelect,
    busy = false,
}: TenantSelectionProps) {
    const active = options.find((o) => o.isActive) ?? options[0];

    if (!active) {
        // Defensive: a multi-tenant user always has options; render nothing
        // rather than a broken control if the list is momentarily empty.
        return null;
    }

    const data = options.map((o) => ({ value: o.id, label: o.name }));

    return (
        <Box maw={520} w="100%">
            <Stack gap="md">
                <Stack gap={4} ta="center">
                    <Title order={1} size="h2" c="primary.7">
                        Okareo MCP
                    </Title>
                    <Title order={2} size="h3">
                        Choose an organization
                    </Title>
                    <Text c="dimmed" size="sm">
                        You have access to more than one Okareo account.
                        Authorize the one this copilot connection should use.
                    </Text>
                </Stack>

                <Select
                    label="Organization to authorize"
                    data={data}
                    value={active.id}
                    onChange={(value) => {
                        if (value && value !== active.id) onSelect(value);
                    }}
                    searchable
                    allowDeselect={false}
                    nothingFoundMessage="No matching organizations"
                    disabled={busy}
                    comboboxProps={{ withinPortal: false }}
                />

                <Button fullWidth disabled={busy} loading={busy} onClick={onAuthorize}>
                    Authorize
                </Button>
            </Stack>
        </Box>
    );
}
