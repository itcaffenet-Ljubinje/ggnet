"""
The web UI's HTTPS certificate (app.serve): what the Settings page shows of it.

scripts/install_ggnet.sh makes a self-signed certificate for the service IP
and the host name once (kept on reinstall); a cafe can put its own
certificate and key in `[web] tls_cert` / `[web] tls_key` instead. A browser
warns about a self-signed one until it is trusted: Settings offers it for
download, to add to the admin PCs' trusted root certificates.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def cert_info(cert: Path) -> dict | None:
    """Subject, names, end date and SHA-256 fingerprint of a PEM certificate; None when unreadable."""
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-subject", "-enddate", "-fingerprint", "-sha256",
             "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout
    subject = re.search(r"^subject=\s*(.*)$", text, re.M)
    end = re.search(r"^notAfter=(.*)$", text, re.M)
    sha = re.search(r"Fingerprint=([0-9A-F:]+)", text)
    names = re.findall(r"(?:DNS|IP Address):([^,\s]+)", text)
    return {
        "subject": subject.group(1).strip() if subject else "",
        "names": names,
        "not_after": end.group(1).strip() if end else "",
        "sha256": sha.group(1) if sha else "",
        "self_signed": _self_signed(cert),
    }


def _self_signed(cert: Path) -> bool:
    try:
        out = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-subject", "-issuer", "-nameopt",
                              "RFC2253"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    lines = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    return lines.get("subject") == lines.get("issuer")


# ── A new self-signed certificate (Settings → General → HTTPS) ─────────

class TlsError(Exception):
    """Shown to the admin as is."""


_DNS_NAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def check_names(names: list[str]) -> list[str]:
    """The addresses for the certificate: IPv4/IPv6 addresses or DNS names, each once."""
    import ipaddress

    out: list[str] = []
    for raw in names:
        name = raw.strip().lower()
        if not name or name in out:
            continue
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if not _DNS_NAME.match(name):
                raise TlsError(f"{raw!r} is neither an IP address nor a DNS name") from None
        out.append(name)
    if not out:
        raise TlsError("give at least one IP address or DNS name")
    if len(out) > 20:
        raise TlsError("at most 20 addresses")
    return out


def regenerate(cert: Path, key: Path, names: list[str], common_name: str) -> None:
    """
    A new self-signed certificate and key for `names`, valid 10 years. Only
    replaces a self-signed certificate: one the cafe put in itself is never
    touched. The old pair is kept as .bak; both files are replaced by renames.
    """
    import os

    if cert.is_file() and not _self_signed(cert):
        raise TlsError("this certificate is not self-signed (the cafe's own): change it in its files, not here")
    names = check_names(names)
    san = ",".join(f"IP:{n}" if _is_ip(n) else f"DNS:{n}" for n in names)
    tmp_cert, tmp_key = cert.with_name(cert.name + ".new"), key.with_name(key.name + ".new")
    try:
        out = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
             "-subj", f"/CN={common_name}/", "-addext", f"subjectAltName={san}",
             "-keyout", str(tmp_key), "-out", str(tmp_cert)],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TlsError(f"openssl failed: {e}") from None
    if out.returncode != 0 or not tmp_cert.is_file() or not tmp_key.is_file():
        tmp_cert.unlink(missing_ok=True)
        tmp_key.unlink(missing_ok=True)
        raise TlsError(f"openssl failed: {out.stderr.strip()[:300]}")
    os.chmod(tmp_key, 0o600)
    for path in (cert, key):
        if path.is_file():
            os.replace(path, path.with_name(path.name + ".bak"))
    os.replace(tmp_key, key)
    os.replace(tmp_cert, cert)


def _is_ip(name: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def trust_script(cert: Path, name: str) -> str:
    """
    A Windows .cmd that adds the certificate to the machine's trusted root
    certificates (run it as administrator once per admin PC). Chrome and Edge
    use that store; Firefox has its own.
    """
    import base64

    der = subprocess.run(["openssl", "x509", "-in", str(cert), "-outform", "DER"], capture_output=True, timeout=10)
    if der.returncode != 0:
        raise TlsError("the certificate cannot be read")
    b64 = base64.b64encode(der.stdout).decode()
    ps = ("$c=New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(,"
          f"[Convert]::FromBase64String('{b64}'));"
          "$s=New-Object System.Security.Cryptography.X509Certificates.X509Store('Root','LocalMachine');"
          "$s.Open('ReadWrite');$s.Add($c);$s.Close();"
          "Write-Host ('Trusted: '+$c.Subject+'  SHA-256 '+$c.GetCertHashString('SHA256'))")
    return "\r\n".join([
        "@echo off",
        f"rem ggNet {name}: trust the web UI's HTTPS certificate on this PC (Chrome, Edge).",
        "rem Run as administrator. Close and reopen the browser afterwards.",
        "net session >nul 2>&1 || (echo Run this file as administrator. & pause & exit /b 1)",
        f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{ps}"',
        "pause",
        "",
    ])
