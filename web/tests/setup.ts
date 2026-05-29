import "@testing-library/jest-dom/vitest";
import { vi, afterEach } from "vitest";

// Stub window.location.assign so handoff success-path tests can assert on the
// redirect target without actually navigating during JSDOM tests.
Object.defineProperty(window, "location", {
    value: {
        ...window.location,
        assign: vi.fn(),
        search: "",
    },
    writable: true,
});

// JSDOM doesn't ship window.matchMedia by default; Mantine's color-scheme
// hook calls it on mount. Stub a no-op MediaQueryList so renders don't crash.
if (typeof window.matchMedia !== "function") {
    Object.defineProperty(window, "matchMedia", {
        writable: true,
        value: (query: string) => ({
            matches: false,
            media: query,
            onchange: null,
            addListener: vi.fn(),
            removeListener: vi.fn(),
            addEventListener: vi.fn(),
            removeEventListener: vi.fn(),
            dispatchEvent: vi.fn(),
        }),
    });
}

// JSDOM also lacks ResizeObserver, which Mantine uses for popover positioning.
if (typeof globalThis.ResizeObserver === "undefined") {
    class ResizeObserverStub {
        observe() {}
        unobserve() {}
        disconnect() {}
    }
    globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

afterEach(() => {
    vi.clearAllMocks();
});
