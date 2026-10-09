import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { machine } from "../test/fixtures";
import { MachineDetails } from "./MachineDetails";

describe("MachineDetails", () => {
  it("shows the hardware and network the agent reported", () => {
    render(
      <MachineDetails
        machine={machine({
          last_seen_at: "2026-10-09T12:00:00Z",
          reported_ip: "192.168.0.21",
          reported_mac: "aa:bb:cc:dd:ee:01",
          link_speed_mbps: 1000,
          agent_version: "0.1.6",
          hardware: {
            nic: "Intel(R) Ethernet I219-V",
            cpu: "AMD Ryzen 5 5600X",
            gpus: ["NVIDIA GeForce RTX 3060"],
            motherboard: "ASUSTeK TUF GAMING B550-PLUS",
            memory_bytes: 16 * 1024 ** 3,
          },
        })}
      />,
    );
    const facts = screen.getByLabelText("Details of pc01");
    for (const text of ["192.168.0.21", "aa:bb:cc:dd:ee:01", "1 Gbit/s", "AMD Ryzen 5 5600X",
                        "NVIDIA GeForce RTX 3060", "ASUSTeK TUF GAMING B550-PLUS", "16.0 GB", "0.1.6"]) {
      expect(facts).toHaveTextContent(text);
    }
  });

  it("says when no agent has reported yet", () => {
    render(<MachineDetails machine={machine()} />);
    expect(screen.getByText(/No agent has reported/)).toBeInTheDocument();
  });
});
