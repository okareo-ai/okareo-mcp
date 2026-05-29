import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
    plugins: [react()],
    test: {
        environment: "jsdom",
        globals: true,
        setupFiles: ["./tests/setup.ts"],
        include: ["tests/unit/**/*.test.{ts,tsx}"],
        exclude: ["tests/e2e/**", "tests/visual/**", "node_modules/**"],
        // frontegg-rest.ts requires a tenant-scoped Frontegg base URL; tests
        // that exercise the real sign-in/sign-up clients need one configured.
        env: {
            NEXT_PUBLIC_FRONTEGG_BASE_URL: "https://app-test.frontegg.com",
        },
    },
    resolve: {
        alias: {
            "@": path.resolve(__dirname, "./src"),
        },
    },
});
