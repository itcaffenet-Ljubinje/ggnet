import { useEffect, useRef, useState } from "react";

import { api, type AgentPackage } from "../api";
import { formatBytes } from "../format";

/**
 * Copy text; navigator.clipboard only exists on https or localhost, and the
 * UI is usually opened over plain http on the LAN, so fall back to execCommand.
 */
export async function copyText(text: string, input: HTMLInputElement | null): Promise<boolean> {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the old way
  }
  if (!input) return false;
  input.select();
  return document.execCommand("copy");
}

/**
 * Settings → Client app: download ggnet-agent or install it with one command
 * on a PC, and whether Disk Mode PCs update it from this server by themselves.
 */
export function ClientApp({ canManage = true }: { canManage?: boolean }) {
  const [pkg, setPkg] = useState<AgentPackage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [saving, setSaving] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .getAgentPackage()
      .then(setPkg)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  async function setAutoUpdate(on: boolean) {
    if (!pkg) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.setAgentAutoUpdate(on);
      setPkg({ ...pkg, auto_update: saved.auto_update });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function copy() {
    if (pkg) setCopied(await copyText(pkg.install_command, input.current));
  }

  return (
    <div className="card" aria-label="Client app">
      <h3 className="card-title">Client app (ggnet-agent)</h3>
      {error && <p className="error-text small">{error}</p>}
      {pkg && !pkg.available && (
        <p className="small warn-text">
          The agent files are not on the server yet. The installer and every update bring them from the ggNet
          release (ggnet-agent-win-x64.zip); a development build has none. Or put ggnet-agent.exe, install.ps1 and
          uninstall.ps1 into <span className="mono">{pkg.directory}</span> yourself.
        </p>
      )}
      {pkg?.available && (
        <>
          <p className="small">
            Version <span className="strong">{pkg.version ?? "unknown"}</span> · server{" "}
            <span className="mono">{pkg.server_url}</span>
          </p>
          <p className="small muted">
            On each PC, open PowerShell as Administrator and run this line. It downloads the agent from this server,
            checks its SHA-256 and installs or updates the service:
          </p>
          <div className="copy-row">
            <input
              ref={input}
              readOnly
              className="mono"
              aria-label="Install command"
              value={pkg.install_command}
              onFocus={(e) => e.target.select()}
            />
            <button type="button" onClick={copy}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <div className="field">
            <label className="check">
              <input type="checkbox" checked={pkg.auto_update} disabled={!canManage || saving}
                     onChange={(e) => setAutoUpdate(e.target.checked)} />
              Update the agent on Disk Mode PCs automatically
            </label>
            <p className="small muted">
              A PC with an older agent downloads this version from the server, checks its SHA-256 and installs it;
              its settings stay and the game disk stays connected. Boot Mode PCs get the agent with their OS image
              (Images → Prepare image).
            </p>
            {pkg.outdated.length > 0 && (
              <p className="small" aria-label="Outdated agents">
                {pkg.auto_update ? "Updating" : "Older agent"} on {pkg.outdated.length} PC
                {pkg.outdated.length === 1 ? "" : "s"}: {pkg.outdated.join(", ")}
              </p>
            )}
          </div>
          <p className="small">
            Or download the files and run <span className="mono">install.ps1 -ServerUrl {pkg.server_url}</span>:{" "}
            <a href="/api/v1/agent/download.zip">Download .zip</a>
          </p>
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Size</th>
                <th>SHA-256</th>
              </tr>
            </thead>
            <tbody>
              {pkg.files.map((f) => (
                <tr key={f.name}>
                  <td>
                    <a href={`/api/v1/agent/download/${f.name}`}>{f.name}</a>
                  </td>
                  <td>{formatBytes(f.size_bytes)}</td>
                  <td className="mono small">{f.sha256}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
