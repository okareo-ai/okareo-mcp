import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MantineProvider } from "@mantine/core";

import { TenantSelection } from "@/components/TenantSelection";
import type { TenantOption } from "@/lib/tenants";

const OPTIONS: TenantOption[] = [
    { id: "org-a", name: "Acme", isActive: true },
    { id: "org-b", name: "Beta Corp", isActive: false },
];

function renderSel(props: Partial<React.ComponentProps<typeof TenantSelection>> = {}) {
    const onAuthorize = props.onAuthorize ?? vi.fn();
    const onSelect = props.onSelect ?? vi.fn();
    render(
        <MantineProvider>
            <TenantSelection
                options={OPTIONS}
                onAuthorize={onAuthorize}
                onSelect={onSelect}
                {...props}
            />
        </MantineProvider>,
    );
    return { onAuthorize, onSelect };
}

describe("TenantSelection (US1/US3)", () => {
    it("pre-sets the picker to the active organization name", () => {
        renderSel();
        expect(screen.getByDisplayValue("Acme")).toBeInTheDocument();
    });

    it("fires onAuthorize when Authorize is clicked", async () => {
        const { onAuthorize } = renderSel();
        await userEvent.click(screen.getByRole("button", { name: /^Authorize$/ }));
        expect(onAuthorize).toHaveBeenCalledTimes(1);
    });

    it("US3: picking a different org from the combobox fires onSelect immediately", async () => {
        const { onSelect } = renderSel();
        await userEvent.click(screen.getByRole("textbox", { name: /Organization to authorize/i }));
        await userEvent.click(screen.getByRole("option", { name: "Beta Corp" }));
        expect(onSelect).toHaveBeenCalledWith("org-b");
    });

    it("does not fire onSelect when the active org is re-picked", async () => {
        const { onSelect } = renderSel();
        await userEvent.click(screen.getByRole("textbox", { name: /Organization to authorize/i }));
        await userEvent.click(screen.getByRole("option", { name: "Acme" }));
        expect(onSelect).not.toHaveBeenCalled();
    });

    it("disables controls while busy", () => {
        renderSel({ busy: true });
        expect(screen.getByRole("button", { name: /^Authorize$/ })).toBeDisabled();
    });
});
