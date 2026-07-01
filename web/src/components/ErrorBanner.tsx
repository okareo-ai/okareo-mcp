"use client";

// Non-leaky status display for the embedded login flow (FR-012, FR-013).
//
// With the Frontegg embedded box (feature 029), credential/MFA/social errors are
// handled inside the box itself. The only states this banner surfaces are the
// ones the page owns: hand-off failures and post-sign-up activation guidance.
// Every variant resolves to a fixed, neutral message so error wording can't be
// used to probe for account existence (US1 scenario 2; US2 scenario 2).

import { Alert } from "@mantine/core";
import { IconAlertTriangle, IconMailFast } from "@tabler/icons-react";

import type { HandoffError } from "@/lib/handoff";

// Informational (non-error) flow states the page may surface.
export type FlowNotice =
    | { kind: "verification_pending" } // US3: sign-up needs email verification (FR-013)
    | { kind: "unknown_error" }; // defensive fallback

export type DisplayableError = HandoffError | FlowNotice;

function messageFor(err: DisplayableError): string {
    switch (err.kind) {
        case "verification_pending":
            return "We've sent a verification email — please confirm your address, then retry from your copilot.";
        case "frontegg_unavailable":
            return "The authentication service is temporarily unavailable. Please retry in a moment.";
        case "expired":
            return "This sign-in session has expired. Please retry from your copilot.";
        case "invalid_token":
            return "Authentication succeeded but the server couldn't validate the returned credentials. Please retry from your copilot.";
        case "tenant_mismatch":
            return "The organization you selected didn't match the credentials returned. Please choose the organization again.";
        case "invalid_request":
            return "Sign-in request was malformed. Please retry from your copilot.";
        case "forbidden":
            return "This sign-in flow expired or originated from an unexpected page. Please retry from your copilot.";
        case "unknown_error":
            return "Sign-in failed for an unexpected reason. Please retry from your copilot.";
    }
}

export function ErrorBanner({ error }: { error: DisplayableError | null }) {
    if (!error) return null;
    const isNotice = error.kind === "verification_pending";
    return (
        <Alert
            role={isNotice ? "status" : "alert"}
            icon={isNotice ? <IconMailFast size={20} /> : <IconAlertTriangle size={20} />}
            color={isNotice ? "blue" : "red"}
            variant="light"
            mt="md"
            data-error-kind={error.kind}
        >
            {messageFor(error)}
        </Alert>
    );
}
