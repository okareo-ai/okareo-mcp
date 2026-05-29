import { defineConfig, devices } from "@playwright/test";

// Playwright config for visual-regression + accessibility + E2E specs.
// Tests are tagged with @visual / @a11y / @e2e for selective runs.
//
// E2E (@e2e) is gated on FRONTEGG_E2E_SANDBOX_* env vars — CI-only.

export default defineConfig({
    testDir: "./tests",
    fullyParallel: true,
    forbidOnly: !!process.env.CI,
    retries: process.env.CI ? 2 : 0,
    workers: process.env.CI ? 2 : undefined,
    reporter: process.env.CI ? "github" : "list",
    use: {
        baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:3000",
        trace: "on-first-retry",
    },
    projects: [
        {
            name: "chromium",
            use: { ...devices["Desktop Chrome"] },
        },
        {
            name: "firefox",
            use: { ...devices["Desktop Firefox"] },
        },
        {
            name: "webkit",
            use: { ...devices["Desktop Safari"] },
        },
    ],
    expect: {
        toHaveScreenshot: {
            // SC-008: ≥95% pixel-diff agreement → ≤5% pixel-diff allowed.
            maxDiffPixelRatio: 0.05,
        },
    },
});
