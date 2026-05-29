// Next.js config for the Okareo MCP embedded login page.
//
// Static export only — the production runtime is the Python MCP container,
// which serves the contents of `out/` at `/login` via Starlette `StaticFiles`.
// No Node.js process runs in production. See specs/021-embedded-login/research.md R5/R6.

/** @type {import('next').NextConfig} */
const nextConfig = {
    output: "export",
    basePath: "/login",
    trailingSlash: false,
    images: {
        // Static export requires the built-in optimizer to be off.
        unoptimized: true,
    },
    reactStrictMode: true,
    // ESLint runs separately via `yarn lint`; avoid blocking the build on
    // lint warnings (especially transitive config-resolution errors from
    // eslint-config-* deps in CI/Docker contexts).
    eslint: {
        ignoreDuringBuilds: true,
    },
    // The page builds against env vars baked at build time (NEXT_PUBLIC_*).
    // Server-side env (FRONTEGG_DOMAIN, MCP_DCR_SIGNING_KEY, etc.) must NEVER
    // be referenced here — they'd leak into the client bundle.
};

export default nextConfig;
