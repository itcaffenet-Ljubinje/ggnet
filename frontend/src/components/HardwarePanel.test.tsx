import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { disk } from "../test/fixtures";
import { HardwarePanel } from "./HardwarePanel";

describe("HardwarePanel", () => {
  it("lists the network card models the image supports", () => {
    const add = vi.fn();
    render(
      <HardwarePanel
        disk={disk({ kind: "os", nic_models: ["Intel(R) Ethernet Connection (2) I219-V", "Realtek PCIe GbE Family Controller"] })}
        onAddHardware={add}
      />,
    );
    expect(screen.getByText("Intel(R) Ethernet Connection (2) I219-V")).toBeInTheDocument();
    expect(screen.getByText("Realtek PCIe GbE Family Controller")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Add new hardware/ }));
    expect(add).toHaveBeenCalled();
  });

  it("says when no PC can boot the image yet", () => {
    render(<HardwarePanel disk={disk({ kind: "os", nic_models: [] })} />);
    expect(screen.getByText(/no PC can boot this image/)).toBeInTheDocument();
  });
});
