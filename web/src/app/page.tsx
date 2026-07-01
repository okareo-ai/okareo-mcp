"use client";

// Embedded login page entry point (features 029 + 030).
//
// Runs INSIDE the client-only FronteggProvider (mounted by layout.tsx). The
// Frontegg embedded box renders every sign-in method enabled centrally
// (email/password + Google + any other social provider); the box also owns
// sign-up and its activation/verify-email screens. This page's job is the
// orchestration the box does not do:
//   1. resolve the one-time `pending` code (URL, or sessionStorage after the
//      Google full-page redirect — research.md R4);
//   2. show the box when unauthenticated;
//   3. once authenticated, EITHER hand off straight to the MCP client
//      (single-org user) OR — feature 030 — let a multi-org user choose which
//      organization to authorize before handing off (FR-001..FR-008).
// Absent/malformed `pending` → a neutral landing; never a hand-off.

import { useCallback, useEffect, useRef, useState } from "react";
import { Box, Center, Container, Loader, Stack, Text, Title } from "@mantine/core";
import { useAuth, useAuthActions, useLoginWithRedirect, useTenantsState } from "@frontegg/react";

import { ErrorBanner, type DisplayableError } from "@/components/ErrorBanner";
import { TenantSelection } from "@/components/TenantSelection";
import { toHandoffRequest } from "@/lib/auth-tokens";
import { postHandoff } from "@/lib/handoff";
import {
    clearPendingCode,
    parsePendingCode,
    resolvePendingCode,
    type PendingCodeStatus,
} from "@/lib/pending";
import { activeTenantId, isMultiTenant, tenantOptions } from "@/lib/tenants";

type Phase =
    | { kind: "loading" }
    | { kind: "no-flow"; status: PendingCodeStatus }
    | { kind: "authenticating" }
    | { kind: "select-tenant" }
    | { kind: "handing-off" }
    | { kind: "error"; error: DisplayableError };

export default function LoginPage() {
    const { isAuthenticated, isLoading, user } = useAuth();
    const loginWithRedirect = useLoginWithRedirect();
    const { switchTenant } = useAuthActions();
    const { tenants } = useTenantsState();

    const [resolved, setResolved] = useState(false);
    const [pendingCode, setPendingCode] = useState<string | null>(null);
    const [urlStatus, setUrlStatus] = useState<PendingCodeStatus>({ kind: "absent" });
    const [phase, setPhase] = useState<Phase>({ kind: "loading" });

    const handoffFired = useRef(false);
    const redirectRequested = useRef(false);
    const switching = useRef(false);

    // Resolve the pending code once on mount: a fresh `?pending=` (persisted for
    // the redirect round-trip) or a previously persisted code on the way back.
    // resolvePendingCode also survives the switchTenant reload (feature 030) for
    // the same reason it survives the social-login redirect.
    useEffect(() => {
        setUrlStatus(parsePendingCode());
        setPendingCode(resolvePendingCode());
        setResolved(true);
    }, []);

    // Perform the hand-off exactly once, scoped to `selectedTenantId` when set.
    const fireHandoff = useCallback(
        (code: string, selectedTenantId?: string) => {
            if (handoffFired.current) return;
            handoffFired.current = true;
            setPhase({ kind: "handing-off" });

            const extraction = toHandoffRequest(code, user);
            if (!extraction.ok) {
                setPhase({ kind: "error", error: { kind: "invalid_token", message: "" } });
                return;
            }
            const request = selectedTenantId
                ? { ...extraction.request, selectedTenantId }
                : extraction.request;

            void postHandoff(request).then((result) => {
                if (result.kind === "success") {
                    clearPendingCode();
                    window.location.assign(result.redirectUrl);
                } else {
                    setPhase({ kind: "error", error: result });
                }
            });
        },
        [user],
    );

    useEffect(() => {
        if (!resolved) return;

        // No flow in progress → neutral landing (distinguish malformed vs absent).
        if (pendingCode === null) {
            setPhase({ kind: "no-flow", status: urlStatus });
            return;
        }

        // Flow in progress — wait for the SDK to settle before deciding.
        if (isLoading) {
            setPhase({ kind: "loading" });
            return;
        }

        // Not signed in → hand control to the embedded box (email/pw + Google).
        if (!isAuthenticated) {
            setPhase({ kind: "authenticating" });
            if (!redirectRequested.current) {
                redirectRequested.current = true;
                void loginWithRedirect();
            }
            return;
        }

        // Signed in. A multi-org user chooses which org to authorize first
        // (feature 030); a single-org user hands off straight through.
        if (handoffFired.current) return;

        if (isMultiTenant(user)) {
            // Stay on the selection screen until the user clicks Authorize.
            setPhase((prev) => (prev.kind === "select-tenant" ? prev : { kind: "select-tenant" }));
            return;
        }

        fireHandoff(pendingCode);
    }, [resolved, pendingCode, urlStatus, isAuthenticated, isLoading, user, loginWithRedirect, fireHandoff]);

    // US1: authorize the currently-active organization and hand off.
    const handleAuthorize = useCallback(() => {
        if (pendingCode === null) return;
        fireHandoff(pendingCode, activeTenantId(user) ?? undefined);
    }, [pendingCode, user, fireHandoff]);

    // US3: re-scope to a different organization via the SDK's own tenant
    // switch (research R1). On success the SDK updates `user` and the render
    // below reflects the new active org; the page stays on `select-tenant`.
    const handleSelect = useCallback(
        (tenantId: string) => {
            if (switching.current) return;
            switching.current = true;
            // silentReload:false so the SDK updates the in-memory user without a
            // full page navigation; the pending code is persisted regardless, so
            // a reload (if the account's config forces one) is still survivable.
            void Promise.resolve(switchTenant({ tenantId, silentReload: false }))
                .then(() => {
                    switching.current = false;
                })
                .catch(() => {
                    switching.current = false;
                    setPhase({
                        kind: "error",
                        error: {
                            kind: "frontegg_unavailable",
                            message:
                                "Couldn't switch organizations. Please try selecting again.",
                        },
                    });
                });
        },
        [switchTenant],
    );

    return (
        <Container size="lg" py="xl">
            <Center mih="80vh">
                {(phase.kind === "loading" ||
                    phase.kind === "authenticating" ||
                    phase.kind === "handing-off") && (
                    <Stack gap="sm" align="center">
                        <Loader size="md" />
                        <Text c="dimmed" size="sm">
                            {phase.kind === "handing-off"
                                ? "Completing sign-in…"
                                : "Signing you in…"}
                        </Text>
                    </Stack>
                )}

                {phase.kind === "select-tenant" && (
                    <TenantSelection
                        options={tenantOptions(user, tenants)}
                        onAuthorize={handleAuthorize}
                        onSelect={handleSelect}
                    />
                )}

                {phase.kind === "no-flow" && phase.status.kind !== "malformed" && (
                    <Box maw={520}>
                        <Stack gap="md" align="center" ta="center">
                            <Title order={1} size="h2" c="primary.7">
                                Okareo MCP
                            </Title>
                            <Title order={2} size="h3">
                                Sign in starts from your copilot
                            </Title>
                            <Text c="dimmed">
                                This page is reached automatically when an MCP-compatible
                                copilot starts an OAuth flow against the Okareo MCP. See the
                                docs for setup instructions for Claude Code, Cursor, and
                                VS Code.
                            </Text>
                        </Stack>
                    </Box>
                )}

                {phase.kind === "no-flow" && phase.status.kind === "malformed" && (
                    <Box maw={520}>
                        <Stack gap="md" align="center" ta="center">
                            <Title order={2} size="h3" c="red.7">
                                This sign-in link is malformed.
                            </Title>
                            <Text c="dimmed">
                                Please retry sign-in from your copilot.
                            </Text>
                        </Stack>
                    </Box>
                )}

                {phase.kind === "error" && (
                    <Box maw={520}>
                        <Stack gap="md" align="center" ta="center">
                            <Title order={2} size="h3">
                                Sign-in couldn&apos;t be completed
                            </Title>
                            <ErrorBanner error={phase.error} />
                        </Stack>
                    </Box>
                )}
            </Center>
        </Container>
    );
}
