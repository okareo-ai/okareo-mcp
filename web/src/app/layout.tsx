"use client";

// Root layout for the embedded login page. Wraps every route in:
//   - MantineProvider (theme + CSS-in-JS context)
//   - Notifications stack (Mantine notifications portal)
//
// No FronteggAppProvider — see specs/021-embedded-login/research.md R1.
// We talk to Frontegg's identity REST API directly from client components;
// no server middleware exists in a static export.

import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./globals.css";

import type { ReactNode } from "react";
import { ColorSchemeScript, MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { cssVariablesResolver, theme } from "@/theme";

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
                    {children}
                </MantineProvider>
            </body>
        </html>
    );
}
