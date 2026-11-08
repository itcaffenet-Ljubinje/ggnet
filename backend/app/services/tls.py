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
