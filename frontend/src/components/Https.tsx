import { useEffect, useState } from "react";
import { Download, ShieldCheck } from "lucide-react";

import { api, type TlsStatus } from "../api";

/**
 * The web UI's HTTPS: the PCs keep plain HTTP (iPXE, agent), people use
 * HTTPS. A self-signed certificate warns once per browser until it is trusted.
 */
export function Https() {
  const [tls, setTls] = useState<TlsStatus | null>(null);
  useEffect(() => {
    api.getTls().then(setTls).catch(() => setTls(null));
  }, []);
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
            <p className="small muted">
              The browser warns once about it. To trust it on an admin PC:{" "}
              <a href="/api/v1/settings/tls/certificate" download="ggnet.crt">
                <Download size={12} /> download the certificate
              </a>
              , open it, Install Certificate → Local Machine → Trusted Root Certification Authorities. Check that the
              SHA-256 above matches.
            </p>
          )}
        </>
      )}
    </div>
  );
}

