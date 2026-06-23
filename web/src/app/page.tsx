"use client";

// Embedded login page entry point (feature 029).
//
// Runs INSIDE the client-only FronteggProvider (mounted by layout.tsx). The
// Frontegg embedded box renders every sign-in method enabled centrally
// (email/password + Google + any other social provider — FR-001/003/005); the
// box also owns sign-up and its activation/verify-email screens (FR-006/006a,
// US3). This page's job is the orchestration the box does not do:
//   1. resolve the one-time `pending` code (URL, or sessionStorage after the
//      Google full-page redirect — research.md R4);
//   2. show the box when unauthenticated;
//   3. once authenticated, post the Frontegg tokens to /oauth/handoff exactly
//      once and navigate back to the MCP client (FR-004/008/009).
// Absent/malformed `pending` → a neutral landing; never a hand-off.

import { useEffect, useRef, useState } from "react";
import { Box, Center, Container, Loader, Stack, Text, Title } from "@mantine/core";
import { useAuth, useLoginWithRedirect } from "@frontegg/react";

import { ErrorBanner, type DisplayableError } from "@/components/ErrorBanner";
import { toHandoffRequest } from "@/lib/auth-tokens";
import { postHandoff } from "@/lib/handoff";
import {
    clearPendingCode,
    parsePendingCode,
    resolvePendingCode,
    type PendingCodeStatus,
} from "@/lib/pending";

type Phase =
    | { kind: "loading" }
    | { kind: "no-flow"; status: PendingCodeStatus }
    | { kind: "authenticating" }
    | { kind: "handing-off" }
    | { kind: "error"; error: DisplayableError };

export default function LoginPage() {
    const { isAuthenticated, isLoading, user } = useAuth();
    const loginWithRedirect = useLoginWithRedirect();

    const [resolved, setResolved] = useState(false);
    const [pendingCode, setPendingCode] = useState<string | null>(null);
    const [urlStatus, setUrlStatus] = useState<PendingCodeStatus>({ kind: "absent" });
    const [phase, setPhase] = useState<Phase>({ kind: "loading" });

    const handoffFired = useRef(false);
    const redirectRequested = useRef(false);

    // Resolve the pending code once on mount: a fresh `?pending=` (persisted for
    // the redirect round-trip) or a previously persisted code on the way back.
    useEffect(() => {
        setUrlStatus(parsePendingCode());
        setPendingCode(resolvePendingCode());
        setResolved(true);
    }, []);

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

        // Signed in → hand off exactly once.
        if (handoffFired.current) return;
        handoffFired.current = true;
        setPhase({ kind: "handing-off" });

        const extraction = toHandoffRequest(pendingCode, user);
        if (!extraction.ok) {
            setPhase({ kind: "error", error: { kind: "invalid_token", message: "" } });
            return;
        }

        void postHandoff(extraction.request).then((result) => {
            if (result.kind === "success") {
                clearPendingCode();
                window.location.assign(result.redirectUrl);
            } else {
                setPhase({ kind: "error", error: result });
            }
        });
    }, [resolved, pendingCode, urlStatus, isAuthenticated, isLoading, user, loginWithRedirect]);

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
