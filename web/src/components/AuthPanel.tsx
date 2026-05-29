"use client";

// Embedded sign-in / sign-up form. v1 is sign-in only (US1); the sign-up
// branch is added in US2 (T040). When the sign-up tab is enabled, this
// component handles both flows with shared submit / handoff plumbing.

import { useEffect, useRef, useState } from "react";
import {
  Anchor,
  Box,
  Button,
  PasswordInput,
  Stack,
  Tabs,
  Text,
  TextInput,
  ThemeIcon,
  Title,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { IconCheck } from "@tabler/icons-react";

import { signIn, signUp } from "@/lib/frontegg-rest";
import type { AuthSuccess } from "@/lib/frontegg-rest";
import { postHandoff } from "@/lib/handoff";
import { ErrorBanner, type DisplayableError } from "./ErrorBanner";

type Mode = "signin" | "signup";

type FormValues = {
  email: string;
  password: string;
  companyName: string;
  name: string;
};

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function validateEmail(value: string): string | null {
  if (!value) return "Email is required";
  if (!EMAIL_PATTERN.test(value)) return "Please enter a valid email";
  return null;
}

function validatePassword(value: string): string | null {
  if (!value) return "Password is required";

  if (value.length < 8) {
    return "Password must be at least 8 characters";
  }

  const checks = [
    /[A-Z]/.test(value), // uppercase
    /[a-z]/.test(value), // lowercase
    /[0-9]/.test(value), // number
    /[^A-Za-z0-9]/.test(value), // special character
  ];

  const passedChecks = checks.filter(Boolean).length;

  if (passedChecks < 2) {
    return "Password must include at least 2 of the following: uppercase, lowercase, number, or special character";
  }

  // Avoid 3 recurring characters in a row
  if (/(.)\1\1/.test(value)) {
    return "Password cannot contain 3 repeating characters in a row";
  }

  return null;
}

function validateCompanyName(value: string): string | null {
  if (!value.trim()) return "Company name is required";
  return null;
}

function validateName(value: string): string | null {
  if (!value.trim()) return "Name is required";
  return null;
}

export interface AuthPanelProps {
  pendingCode: string;
  /** Allow disabling the sign-up tab for v1; defaults to enabled. */
  enableSignUp?: boolean;
}

export function AuthPanel({
  pendingCode,
  enableSignUp = true,
}: AuthPanelProps) {
  const [mode, setMode] = useState<Mode>("signin");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<DisplayableError | null>(null);
  const [success, setSuccess] = useState<{ redirectUrl: string } | null>(null);

  // The validate config below is captured by useForm on first render, so a
  // mode-dependent validator must read mode via a ref to stay current.
  const modeRef = useRef<Mode>(mode);
  modeRef.current = mode;

  const form = useForm<FormValues>({
    initialValues: { email: "", password: "", companyName: "", name: "" },
    validate: {
      email: validateEmail,
      password: validatePassword,
      // Company name and name are required only on the sign-up tab
      // (FR-018 / FR-019); the sign-in tab never renders these fields.
      companyName: (value) =>
        modeRef.current === "signup" ? validateCompanyName(value ?? "") : null,
      name: (value) =>
        modeRef.current === "signup" ? validateName(value ?? "") : null,
    },
  });

  // Render the success state first, then trigger navigation on a short
  // delay. Why the delay: the MCP-client redirect URI may use a custom
  // URI scheme (e.g., `cursor://...`) which hands the OAuth code off to
  // the OS-level handler but doesn't navigate the browser tab. In that
  // case the page stays on screen and the user needs to see "You're
  // signed in". For standard HTTPS callbacks (Claude Code etc.) the
  // browser navigates away before the success state is visible — fine.
  //
  // The cleanup function cancels the timer if the component unmounts
  // before it fires (e.g., test teardown, browser back-button) so the
  // navigation never happens against a stale mount.
  useEffect(() => {
    if (!success) return;
    const timer = setTimeout(() => {
      window.location.assign(success.redirectUrl);
    }, 250);
    return () => clearTimeout(timer);
  }, [success]);

  async function handleSuccess(tokens: AuthSuccess) {
    const result = await postHandoff({
      pendingCode,
      fronteggAccessToken: tokens.accessToken,
      fronteggRefreshToken: tokens.refreshToken,
      fronteggExpiresIn: tokens.expiresIn,
    });
    if (result.kind === "success") {
      setSuccess({ redirectUrl: result.redirectUrl });
      return;
    }
    setError(result);
    setSubmitting(false);
  }

  async function onSubmit(values: FormValues) {
    setError(null);
    setSubmitting(true);

    if (mode === "signin") {
      const result = await signIn({
        email: values.email,
        password: values.password,
      });
      if (result.kind === "tokens") {
        await handleSuccess(result);
        return;
      }
      setError(result);
      setSubmitting(false);
      return;
    }

    // sign-up
    const result = await signUp({
      email: values.email,
      password: values.password,
      companyName: values.companyName.trim(),
      name: values.name.trim(),
    });
    if (result.kind === "tokens") {
      await handleSuccess(result);
      return;
    }
    setError(result);
    setSubmitting(false);
  }

  if (success !== null) {
    return (
      <Box
        component="section"
        role="status"
        aria-live="polite"
        aria-label="Sign-in successful"
        p="xl"
        style={{
          background: "white",
          borderRadius: "20px",
          boxShadow: "0 4px 6px rgba(0, 0, 0, 0.08)",
          maxWidth: 440,
          width: "100%",
        }}
      >
        <Stack gap="lg" align="center" ta="center">
          <ThemeIcon size={72} radius="xl" color="green" variant="light">
            <IconCheck size={48} />
          </ThemeIcon>
          <Title order={2} size="h3">
            You&apos;re signed in
          </Title>
          <Text c="dimmed">
            Return to your copilot to continue. You can close this tab.
          </Text>
          <Text size="xs" c="dimmed">
            If your copilot didn&apos;t pick up the sign-in automatically,
            reload it once.
          </Text>
        </Stack>
      </Box>
    );
  }

  return (
    <Box
      component="section"
      aria-label={mode === "signin" ? "Sign in" : "Create your Okareo account"}
      p="xl"
      style={{
        background: "white",
        borderRadius: "20px",
        boxShadow: "0 4px 6px rgba(0, 0, 0, 0.08)",
        maxWidth: 440,
        width: "100%",
      }}
    >
      <Stack gap="lg">
        <Title order={2} size="h3">
          {mode === "signin"
            ? "Sign in to Okareo MCP"
            : "Create your Okareo account"}
        </Title>

        {enableSignUp ? (
          <Tabs
            value={mode}
            onChange={(v) => {
              if (v === "signin" || v === "signup") {
                setMode(v);
                setError(null);
              }
            }}
            keepMounted={false}
          >
            <Tabs.List grow>
              <Tabs.Tab value="signin">Sign in</Tabs.Tab>
              <Tabs.Tab value="signup">Create account</Tabs.Tab>
            </Tabs.List>
          </Tabs>
        ) : null}

        <form onSubmit={form.onSubmit(onSubmit)} noValidate>
          <Stack gap="md">
            {mode === "signup" && (
              <TextInput
                label="Name"
                placeholder="Your full name"
                autoComplete="name"
                required
                disabled={submitting}
                {...form.getInputProps("name")}
              />
            )}
            <TextInput
              label="Email"
              type="email"
              placeholder="you@example.com"
              autoComplete="email"
              required
              disabled={submitting}
              {...form.getInputProps("email")}
            />
            {mode === "signup" && (
              <TextInput
                label="Company name"
                placeholder="Your company or team"
                autoComplete="organization"
                required
                disabled={submitting}
                {...form.getInputProps("companyName")}
              />
            )}
            <PasswordInput
              label="Password"
              placeholder={
                mode === "signin" ? "Your password" : "At least 8 characters"
              }
              autoComplete={
                mode === "signin" ? "current-password" : "new-password"
              }
              required
              disabled={submitting}
              {...form.getInputProps("password")}
            />

            <ErrorBanner error={error} />

            <Button
              type="submit"
              color="primary"
              size="md"
              loading={submitting}
              fullWidth
            >
              {mode === "signin" ? "Sign in" : "Create account"}
            </Button>

            {mode === "signin" && enableSignUp && (
              <Anchor
                component="button"
                type="button"
                onClick={() => {
                  setMode("signup");
                  setError(null);
                }}
                disabled={submitting}
                size="sm"
                ta="center"
              >
                New to Okareo? Create an account.
              </Anchor>
            )}
            {mode === "signup" && (
              <Anchor
                component="button"
                type="button"
                onClick={() => {
                  setMode("signin");
                  setError(null);
                }}
                disabled={submitting}
                size="sm"
                ta="center"
              >
                Already have an account? Sign in.
              </Anchor>
            )}
          </Stack>
        </form>
      </Stack>
    </Box>
  );
}
