"use client";

// Mirror of appfrontend/src/theme.ts so the embedded login page renders with
// the same Mantine theme tokens (primary virtualColor, breakpoints, status
// colors) as the main Okareo web app.

import { createTheme, virtualColor } from "@mantine/core";

export const STATUS_COLORS = {
    status_ok: "var(--mantine-color-blue-5)",
    status_success: "var(--mantine-color-green-6)",
    status_warning: "var(--mantine-color-yellow-6)",
    status_error: "var(--mantine-color-red-6)",
} as const;

export const theme = createTheme({
    colors: {
        primary: virtualColor({
            name: "primary",
            dark: "blue",
            light: "blue",
        }),
    },
    breakpoints: {
        xs: "30em",
        sm: "48em",
        md: "64em",
        lg: "74em",
        xl: "90em",
    },
});

export const cssVariablesResolver = () => ({
    variables: {
        "--status-color-ok": STATUS_COLORS.status_ok,
        "--status-color-success": STATUS_COLORS.status_success,
        "--status-color-warning": STATUS_COLORS.status_warning,
        "--status-color-error": STATUS_COLORS.status_error,
    },
    light: {},
    dark: {},
});
