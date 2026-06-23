"use client";

// Root layout for the embedded login page. Wraps every route in:
//   - MantineProvider (theme + CSS-in-JS context) — server-renderable
//   - Notifications stack (Mantine notifications portal)
//   - Frontegg embedded-login boundary (client-only) — feature 029
//
// The Frontegg provider (@frontegg/react) is a browser-only SPA SDK, so it is
// loaded via next/dynamic({ ssr: false }) and never runs during the static
// export prerender. The /login page renders inside it (and thus only in the
// browser, where useAuth has a provider). See specs/029-google-oauth-embedded.

import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./globals.css";

import type { ReactNode } from "react";
import { ColorSchemeScript, Loader, MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import dynamic from "next/dynamic";
import { cssVariablesResolver, theme } from "@/theme";

// ssr:false keeps @frontegg/react out of the prerender (T004 spike).
const FronteggBoundary = dynamic(() => import("./providers"), {
    ssr: false,
    loading: () => <Loader size="sm" />,
});

export default function RootLayout({ children }: { children: ReactNode }) {
    return (
        <html lang="en">
            <head>
                <title>Sign in to Okareo MCP</title>
                <meta
                    name="description"
                    content="Connect your AI copilot to Okareo's evaluation platform — sign in or create an Okareo account to start using the Okareo MCP."
                />
                <meta
                    name="viewport"
                    content="minimum-scale=1, initial-scale=1, width=device-width"
                />
                <ColorSchemeScript defaultColorScheme="light" />
            </head>
            <body>
                <MantineProvider
                    theme={theme}
                    cssVariablesResolver={cssVariablesResolver}
                    defaultColorScheme="light"
                >
                    <Notifications />
                    <FronteggBoundary>{children}</FronteggBoundary>
                </MantineProvider>
            </body>
        </html>
    );
}
