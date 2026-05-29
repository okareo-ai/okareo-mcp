"use client";

// Non-leaky error display for the embedded login flow (FR-012).
//
// Maps the discriminated union of auth errors → a fixed, neutral user-facing
// message. We intentionally do NOT include free-form messages from
// Frontegg / the handoff endpoint here — every variant resolves to a
// canonical message so attackers can't probe for account-existence by
// reading error wording differences (US1 acceptance scenario 2; US2
// acceptance scenario 2).

import { Alert } from "@mantine/core";
import { IconAlertTriangle } from "@tabler/icons-react";

import type { AuthError } from "@/lib/frontegg-rest";
import type { HandoffError } from "@/lib/handoff";

export type DisplayableError = AuthError | HandoffError;

function messageFor(err: DisplayableError): string {
    switch (err.kind) {
        case "invalid_credentials":
            return "We couldn't sign you in with those credentials. Check your email and password and try again.";
        case "signup_conflict":
            return "If an account exists for this email, please sign in instead.";
        case "verification_pending":
            return "We've sent a verification email — please confirm your address, then sign in.";
        case "mfa_required":
            return "Your account requires multi-factor authentication. For now, please sign in via the Okareo dashboard and retry from your copilot.";
        case "frontegg_unavailable":
            return "The authentication service is temporarily unavailable. Please retry in a moment.";
        case "expired":
            return "This sign-in session has expired. Please retry from your copilot.";
        case "invalid_token":
            return "Authentication succeeded but the server couldn't validate the returned credentials. Please retry from your copilot.";
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
    return (
        <Alert
            role="alert"
            icon={<IconAlertTriangle size={20} />}
            color="red"
            variant="light"
            mt="md"
            data-error-kind={error.kind}
        >
            {messageFor(error)}
        </Alert>
    );
}
