import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { machine } from "../test/fixtures";
import { ago, AgentState } from "./AgentState";

const NOW = Date.parse("2026-10-07T12:10:00Z");

describe("ago", () => {
  it("formats seconds, minutes, hours and days", () => {
    expect(ago("2026-10-07T12:09:30Z", NOW)).toBe("30s ago");
    expect(ago("2026-10-07T12:05:00Z", NOW)).toBe("5 min ago");
    expect(ago("2026-10-07T09:10:00Z", NOW)).toBe("3 h ago");
    expect(ago("2026-10-04T12:10:00Z", NOW)).toBe("3 d ago");
  });
});

describe("AgentState", () => {
  it("shows when no agent has reported", () => {
    render(<AgentState machine={machine()} now={NOW} />);
    expect(screen.getByText("No agent yet")).toBeInTheDocument();
  });

  it("is online with a recent heartbeat and shows the disk state", () => {
    const m = machine({
      last_seen_at: "2026-10-07T12:09:40Z",
      agent_version: "0.1.0",
      reported_iqn: "iqn.1991-05.com.microsoft:pc01",
      iscsi_connected: true,
    });
    render(<AgentState machine={m} now={NOW} />);
    expect(screen.getByText("Online")).toBeInTheDocument();
    expect(screen.getByText("Disk connected")).toBeInTheDocument();
    expect(screen.getByText(/20s ago · agent 0\.1\.0/)).toBeInTheDocument();
    expect(screen.queryByText(/PC reports IQN/)).not.toBeInTheDocument();
  });

  it("is offline after 90 s and hides the stale disk state", () => {
    const m = machine({ last_seen_at: "2026-10-07T12:05:00Z", iscsi_connected: true });
    render(<AgentState machine={m} now={NOW} />);
    expect(screen.getByText("Offline")).toBeInTheDocument();
    expect(screen.queryByText("Disk connected")).not.toBeInTheDocument();
  });

  it("warns when the PC reports a different IQN than the ACL expects", () => {
    const m = machine({
      last_seen_at: "2026-10-07T12:09:40Z",
      reported_iqn: "iqn.1991-05.com.microsoft:pc01.cafe.local",
      iscsi_connected: false,
    });
    render(<AgentState machine={m} now={NOW} />);
    expect(screen.getByText("Disk not connected")).toBeInTheDocument();
    expect(screen.getByText(/PC reports IQN iqn\.1991-05\.com\.microsoft:pc01\.cafe\.local/))
      .toBeInTheDocument();
  });
});
