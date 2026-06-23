"use client";

// Client-only Frontegg embedded-login boundary (feature 029).
//
// @frontegg/react is a browser-only SPA SDK (internally a react-router
// BrowserRouter). It must never render during the static-export prerender, so
// this module is mounted via next/dynamic({ ssr: false }) from layout.tsx.
// Everything under it (the /login page) therefore runs only in the browser,
// where useAuth() has a provider — see research.md R2 and the T004 spike.

import type { ReactNode } from "react";
import { FronteggProvider } from "@frontegg/react";

import {
    contextOptions,
    FRONTEGG_BASENAME,
    FRONTEGG_OAUTH_REDIRECT_PATH,
    loginBoxTheme,
} from "@/lib/frontegg-config";

export default function Providers({ children }: { children: ReactNode }) {
    return (
        <FronteggProvider
            contextOptions={contextOptions}
            hostedLoginBox={false}
            authOptions={{
                keepSessionAlive: true,
                // Pin the OAuth/social redirect path explicitly (configurable)
                // rather than relying on the SDK default, so the registered
                // Frontegg redirect URL and the value we send always agree.
                routes: { hostedLoginRedirectUrl: FRONTEGG_OAUTH_REDIRECT_PATH },
            }}
            themeOptions={loginBoxTheme}
            basename={FRONTEGG_BASENAME}
        >
            {children}
        </FronteggProvider>
    );
}
