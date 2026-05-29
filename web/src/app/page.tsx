"use client";

// Embedded login page entry point.
//
// Three branches per specs/021-embedded-login/contracts/login-page-contract.md §1.2:
//   pending valid    → render <AuthPanel/> (sign-in form; sign-up tab in US2)
//   pending absent   → "install in your copilot first" landing
//   pending malformed → "malformed link" error
//
// MCPBenefits side panel lands in US3 (T045/T046).

import { useEffect, useState } from "react";
import { Box, Center, Container, Stack, Text, Title } from "@mantine/core";
import { AuthPanel } from "@/components/AuthPanel";
import { parsePendingCode, type PendingCodeStatus } from "@/lib/pending";

export default function LoginPage() {
    const [status, setStatus] = useState<PendingCodeStatus | null>(null);

    useEffect(() => {
        setStatus(parsePendingCode());
    }, []);

    return (
        <Container size="lg" py="xl">
            <Center mih="80vh">
                {status === null && (
                    <Text c="dimmed" size="sm">
                        Loading…
                    </Text>
                )}

                {status?.kind === "valid" && (
                    <AuthPanel pendingCode={status.code} />
                )}

                {status?.kind === "absent" && (
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

                {status?.kind === "malformed" && (
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
            </Center>
        </Container>
    );
}
