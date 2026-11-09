import { useEffect, useState } from "react";
import { Download, RefreshCw, ShieldCheck } from "lucide-react";

import { api, type TlsStatus } from "../api";

/**
 * The web UI's HTTPS: the PCs keep plain HTTP (iPXE, agent), people use
 * HTTPS. A self-signed certificate warns once per browser until it is trusted.
 */
export function Https({ canManage = true }: { canManage?: boolean }) {
  const [tls, setTls] = useState<TlsStatus | null>(null);
  const [names, setNames] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    api
      .getTls()
      .then((t) => {
        setTls(t);
        setNames((t.names ?? []).join(", "));
      })
      .catch(() => setTls(null));
  }, []);

  async function renew() {
    const list = names.split(",").map((n) => n.trim()).filter(Boolean);
    if (
      !window.confirm(
        `Make a new certificate for ${list.join(", ")}?\n\nggNet restarts to use it (a few seconds; the PCs keep ` +
          "their disks). Every admin PC must trust the new certificate again (download it below).",
      )
    )
      return;
    setBusy(true);
    setError(null);
    try {
      const t = await api.newCertificate(list);
      setTls(t);
      setNames((t.names ?? []).join(", "));
      setNotice(t.restarting ? "New certificate made; ggNet restarts to use it. Reload this page in a few seconds." : "New certificate made.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  if (!tls) return null;
  return (
    <div className="card" aria-label="HTTPS">
      <h3 className="card-title title-with-icon">
        <ShieldCheck size={16} aria-hidden />
        HTTPS for the web UI
      </h3>
      {!tls.enabled ? (
        <p className="small warn-text">
          The web UI runs on plain HTTP: run the installer again to set up HTTPS (the PCs stay on HTTP).
        </p>
      ) : (
        <>
          <p className="small">
            The web UI: <span className="mono">{tls.url}</span>. The PCs (iPXE, the agent) keep using plain HTTP;
            a browser that opens it is sent here.
          </p>
          <p className="small muted">
            {tls.self_signed ? "Self-signed certificate" : "Certificate"} for{" "}
            <span className="mono">{tls.names.join(", ") || tls.subject}</span>, valid until {tls.not_after}.
            <br />
            SHA-256 <span className="mono small">{tls.sha256}</span>
          </p>
          {tls.self_signed && (
            <>
              <p className="small">
                The browser warns once about it. To trust it on an admin PC (Chrome, Edge):{" "}
                <a href="/api/v1/settings/tls/trust.cmd" download="ggnet-trust-certificate.cmd">
                  <Download size={12} /> Download for Windows
                </a>{" "}
                and run it as administrator, then close and reopen the browser. Or{" "}
                <a href="/api/v1/settings/tls/certificate" download="ggnet.crt">
                  download the certificate
                </a>{" "}
                and install it by hand (Local Machine → Trusted Root Certification Authorities). Firefox keeps its own
                list. Check that the SHA-256 above matches.
              </p>
              {canManage && (
                <div className="form-stacked">
                  <label>
                    Addresses in the certificate
                    <input
                      value={names}
                      onChange={(e) => setNames(e.target.value)}
                      aria-label="Addresses in the certificate"
                      placeholder="192.168.0.40, ggnet.cafe.local"
                    />
                    <span className="form-hint">
                      IP addresses and DNS names, separated by commas: the ones you open the web UI with.
                    </span>
                  </label>
                  <div className="actions-row">
                    <button type="button" className="secondary" disabled={busy || !names.trim()} onClick={renew}>
                      <RefreshCw size={14} />
                      {busy ? "Making…" : "Make a new certificate"}
                    </button>
                  </div>
                </div>
              )}
              {error && <p className="small error-text">{error}</p>}
              {notice && (
                <p className="small" role="status">
                  {notice}
                </p>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}

