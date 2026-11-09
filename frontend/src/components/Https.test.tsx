import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Https } from "./Https";

function stub(tls: unknown) {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async () => new Response(JSON.stringify(tls), { status: 200 })));
}

describe("Https", () => {
  it("shows the web UI's HTTPS and offers its certificate to trust", async () => {
    stub({
      enabled: true, url: "https://192.168.0.40:8443", subject: "CN=ggNet pve", names: ["192.168.0.40", "pve"],
      not_after: "Nov  5 12:00:00 2036 GMT", sha256: "AB:CD", self_signed: true,
    });
    render(<Https />);
    expect(await screen.findByText("https://192.168.0.40:8443")).toBeInTheDocument();
    expect(screen.getByText("192.168.0.40, pve")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /download the certificate/ })).toHaveAttribute(
      "href", "/api/v1/settings/tls/certificate");
  });

  it("says when the web UI still runs on plain HTTP", async () => {
    stub({ enabled: false });
    render(<Https />);
    expect(await screen.findByText(/The web UI runs on plain HTTP/)).toBeInTheDocument();
  });
});
