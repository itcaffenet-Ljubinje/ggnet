import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

  it("makes a new self-signed certificate for other addresses and offers the Windows script", async () => {
    const TLS = {
      enabled: true, url: "https://192.168.0.40:8443", subject: "CN=ggNet pve", names: ["192.168.0.40", "pve"],
      not_after: "Nov  5 12:00:00 2036 GMT", sha256: "AB:CD", self_signed: true,
    };
    const mock = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) =>
      new Response(JSON.stringify(init?.method === "PUT"
        ? { ...TLS, names: ["192.168.0.40", "ggnet.cafe.local"], restarting: true } : TLS), { status: 200 }));
    vi.stubGlobal("fetch", mock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Https />);
    expect(await screen.findByRole("link", { name: /Download for Windows/ })).toHaveAttribute(
      "href", "/api/v1/settings/tls/trust.cmd");
    const input = screen.getByLabelText("Addresses in the certificate");
    expect(input).toHaveValue("192.168.0.40, pve");
    await userEvent.clear(input);
    await userEvent.type(input, "192.168.0.40, ggnet.cafe.local");
    await userEvent.click(screen.getByRole("button", { name: /Make a new certificate/ }));
    const put = mock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT")!;
    expect(JSON.parse((put[1] as RequestInit).body as string)).toEqual({ names: ["192.168.0.40", "ggnet.cafe.local"] });
    expect(await screen.findByText(/ggNet restarts to use it/)).toBeInTheDocument();
  });
});
