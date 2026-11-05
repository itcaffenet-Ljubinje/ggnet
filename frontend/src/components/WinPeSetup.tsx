import { useCallback, useEffect, useState, type FormEvent } from "react";
import { FolderUp, RefreshCw, Trash2 } from "lucide-react";

import { api, uploadDriverPackage, type WinPe, type WinPePackage } from "../api";
import { formatBytes } from "../format";
import { ErrorBanner } from "./ErrorBanner";

const PACKAGE_NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const SHOWN_DEVICES = 4;

/** "Intel(R) Ethernet Connection I219-V" → "Intel Ethernet Connection I219-V". */
function shortName(name: string): string {
  return name.replace(/\((R|TM)\)/gi, "").replace(/\s+/g, " ").trim();
}

/** A folder picked with webkitdirectory: only its own files, not its subfolders'. */
export function topLevelFiles(files: FileList | File[]): File[] {
  return Array.from(files).filter((f) => {
    const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath ?? "";
    return rel === "" || rel.split("/").length === 2;
  });
}

/**
 * Settings → Hardware scan: Windows PE for Add new hardware (copied out of a
 * Windows ISO on the server) and the network drivers it loads.
 */
export function WinPeSetup() {
  const [pe, setPe] = useState<WinPe | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPe(await api.getWinPe());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function act(what: string, call: () => Promise<unknown>) {
    setBusy(what);
    setError(null);
    try {
      await call();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setBusy(null);
  }

  return (
    <>
      <WindowsPe pe={pe} busy={busy} error={error} setError={setError} act={act} reload={load} />
      {pe && <DriverLibrary pe={pe} busy={busy} act={act} />}
      {pe && pe.pc_chips.length > 0 && <PcChips pe={pe} />}
    </>
  );
}

interface PartProps {
  pe: WinPe;
  busy: string | null;
  act: (what: string, call: () => Promise<unknown>) => Promise<void>;
}

function WindowsPe({
  pe,
  busy,
  error,
  setError,
  act,
  reload,
}: Omit<PartProps, "pe"> & {
  pe: WinPe | null;
  error: string | null;
  setError: (e: string | null) => void;
  reload: () => Promise<void>;
}) {
  const [iso, setIso] = useState("");
  useEffect(() => {
    if (pe && !pe.isos.some((i) => i.path === iso)) setIso(pe.isos[0]?.path ?? "");
  }, [pe, iso]);

  const wimboot = pe?.files.find((f) => f.name === "wimboot");
  return (
    <div className="card" aria-label="Windows PE">
      <h3 className="card-title">Hardware scan (Windows PE)</h3>
      <ErrorBanner message={error} onClose={() => setError(null)} />
      <p className="small muted">
        Add new hardware starts the new PC in Windows PE once, to read its network card. Windows PE is taken from a
        Windows installation ISO on this server; ggNet does not ship Microsoft&apos;s files.
      </p>
      {pe && (
        <>
          <div className="pxe-status">
            <span className={`badge ${pe.ready ? "badge-provisioned" : "badge-warn"}`}>
              {pe.ready ? "Ready" : "Not ready"}
            </span>
            <span className="mono small">{pe.folder}</span>
            <span className="pxe-actions">
              <button type="button" className="icon-btn" onClick={reload} disabled={busy !== null} aria-label="Refresh">
                <RefreshCw size={14} />
              </button>
            </span>
          </div>
          <p className="muted small">
            {pe.files.map((f, i) => (
              <span key={f.name}>
                {i > 0 && ", "}
                <span className={f.present ? undefined : "error-text"}>
                  {f.name} {f.present ? `✓ ${formatBytes(f.size ?? 0)}` : "missing"}
                </span>
              </span>
            ))}
            {", "}
            <span className={pe.scanner ? undefined : "error-text"}>
              ggnet-scan.exe {pe.scanner ? "✓" : "missing"}
            </span>
          </p>
          {wimboot && !wimboot.present && (
            <p className="small warn-text">wimboot is missing: run the installer again, it downloads wimboot.</p>
          )}
          {!pe.scanner && (
            <p className="small warn-text">
              ggnet-scan.exe is missing: put it (from the release) next to the agent files (Settings → Client app).
            </p>
          )}

          <form
            className="copy-row"
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              if (iso) act("iso", () => api.winPeFromIso(iso));
            }}
          >
            <select aria-label="Windows ISO" value={iso} onChange={(e) => setIso(e.target.value)} disabled={!pe.isos.length}>
              {pe.isos.length === 0 && <option value="">No .iso file found</option>}
              {pe.isos.map((i) => (
                <option key={i.path} value={i.path}>
                  {i.name} ({formatBytes(i.size)})
                </option>
              ))}
            </select>
            <button type="submit" disabled={!iso || busy !== null}>
              {busy === "iso" ? "Copying…" : pe.files.slice(1).every((f) => f.present) ? "Take again" : "Take Windows PE"}
            </button>
          </form>
          <p className="small muted">
            A Windows 10 or 11 ISO (64-bit) from <span className="mono">{pe.iso_folders.join(", ")}</span>. Upload one
            in Proxmox: local storage → ISO Images → Upload. Copying takes about a minute.
          </p>
        </>
      )}
    </div>
  );
}

function DriverLibrary({ pe, busy, act }: PartProps) {
  const [name, setName] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [progress, setProgress] = useState<string | null>(null);
  const [inputKey, setInputKey] = useState(0);

  const nameOk = PACKAGE_NAME.test(name) && !pe.packages.some((p) => p.name === name);

  async function add(e: FormEvent) {
    e.preventDefault();
    await act("upload", async () => {
      await uploadDriverPackage(name, files, (done, total) => setProgress(`${done}/${total}`));
      setName("");
      setFiles([]);
      setInputKey((k) => k + 1);
    });
    setProgress(null);
  }

  return (
    <div className="card" aria-label="Network drivers for Windows PE">
      <h3 className="card-title">Network drivers for Windows PE</h3>
      <p className="small muted">
        Windows PE has few network drivers of its own. Add the vendor&apos;s driver folder for Windows x64 (INF, SYS
        and CAT files), downloaded from the vendor&apos;s site: e.g. Intel Ethernet Adapter Complete Driver Pack →{" "}
        <span className="mono">PRO1000\Winx64\NDIS68</span> (I217/I218/I219, I210/I211) and{" "}
        <span className="mono">PRO2500\Winx64\NDIS68</span> (I225/I226); Realtek Win10/Win11 Auto Installation
        Program (NDIS) → <span className="mono">WIN10\WinPE\64</span>.
      </p>
      {pe.packages.length === 0 ? (
        <p className="small warn-text">No driver package yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Package</th>
              <th>Network cards</th>
              <th>Files</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {pe.packages.map((p) => (
              <PackageRow key={p.name} pkg={p} busy={busy} act={act} />
            ))}
          </tbody>
        </table>
      )}

      <form className="driver-upload" onSubmit={add}>
        <label>
          Package name
          <input
            value={name}
            onChange={(e) => setName(e.target.value.toLowerCase())}
            placeholder="intel-pro1000"
            aria-invalid={name !== "" && !nameOk}
          />
        </label>
        <label>
          Driver folder
          <input
            key={inputKey}
            type="file"
            aria-label="Driver folder"
            {...{ webkitdirectory: "", directory: "" }}
            onChange={(e) => setFiles(e.target.files ? topLevelFiles(e.target.files) : [])}
          />
        </label>
        <button type="submit" disabled={!nameOk || files.length === 0 || busy !== null}>
          <FolderUp size={14} />
          {busy === "upload" ? `Adding… ${progress ?? ""}` : `Add${files.length ? ` (${files.length} files)` : ""}`}
        </button>
      </form>
      <p className="small muted">
        Stored in <span className="mono">{pe.drivers_folder}</span>. Only the folder&apos;s own files are taken, not its
        subfolders.
      </p>
    </div>
  );
}

function PackageRow({ pkg, busy, act }: { pkg: WinPePackage } & Omit<PartProps, "pe">) {
  const [all, setAll] = useState(false);
  const names = Array.from(new Set(pkg.devices.map((d) => shortName(d.name))));
  const shown = all ? names : names.slice(0, SHOWN_DEVICES);
  return (
    <tr>
      <td className="mono">{pkg.name}</td>
      <td className="small">
        {shown.join(", ")}
        {names.length > SHOWN_DEVICES && (
          <>
            {" "}
            <button type="button" className="link-btn small" onClick={() => setAll(!all)}>
              {all ? "less" : `+${names.length - SHOWN_DEVICES} more`}
            </button>
          </>
        )}
      </td>
      <td className="small">
        {pkg.files} · {formatBytes(pkg.size)}
      </td>
      <td>
        <button
          type="button"
          className="icon-btn"
          aria-label={`Remove ${pkg.name}`}
          disabled={busy !== null}
          onClick={() => {
            if (window.confirm(`Remove the driver package ${pkg.name}?`))
              act("remove", () => api.removeDriverPackage(pkg.name));
          }}
        >
          <Trash2 size={14} />
        </button>
      </td>
    </tr>
  );
}

function PcChips({ pe }: { pe: WinPe }) {
  return (
    <div className="card" aria-label="Network chips of the PCs">
      <h3 className="card-title">Network chips of the PCs</h3>
      <p className="small muted">
        The boot network cards the PCs reported. A chip without a package works in Windows PE only if Windows PE has
        its own driver for it.
      </p>
      <table>
        <thead>
          <tr>
            <th>Chip</th>
            <th>Network card</th>
            <th>Driver package</th>
            <th>PCs</th>
          </tr>
        </thead>
        <tbody>
          {pe.pc_chips.map((c) => (
            <tr key={c.chip}>
              <td className="mono small">
                {c.vendor} {c.chip}
              </td>
              <td className="small">{c.name ? shortName(c.name) : "—"}</td>
              <td className={c.package ? "mono small" : "small warn-text"}>{c.package ?? "none"}</td>
              <td>{c.pcs}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
