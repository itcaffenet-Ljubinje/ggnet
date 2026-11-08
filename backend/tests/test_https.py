"""HTTPS for the web UI, HTTP for the PCs (app.serve, app.main's redirect, Settings → certificate)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import serve


@pytest.fixture
def cert(tmp_path) -> tuple[Path, Path]:
    crt, key = tmp_path / "ggnet.crt", tmp_path / "ggnet.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
                    "-subj", "/CN=pve", "-addext", "subjectAltName=IP:192.168.0.40,DNS:pve",
                    "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
    return crt, key


def _cfg(crt: Path | None = None, key: Path | None = None) -> dict:
    web = {"bind": "192.168.0.40", "port": 8088}
    if crt:
        web.update(https_port=8443, tls_cert=str(crt), tls_key=str(key))
    return {"web": web, "network": {"server_ip": "192.168.0.40"}}


def test_without_a_certificate_only_http_runs(tmp_path):
    assert serve.tls(_cfg()) is None
    assert serve.tls(_cfg(tmp_path / "missing.crt", tmp_path / "missing.key")) is None
    [http] = serve.configs(_cfg())
    assert (http.host, http.port, http.ssl_certfile) == ("192.168.0.40", 8088, None)


def test_with_a_certificate_the_web_ui_gets_https_and_the_workers_start_once(cert):
    http, https = serve.configs(_cfg(*cert))
    assert (http.port, http.lifespan) == (8088, "auto")
    assert (https.port, https.ssl_certfile, https.lifespan) == (8443, str(cert[0]), "off")


def test_people_are_sent_to_https_and_the_pcs_stay_on_http(client, cert, monkeypatch):
    monkeypatch.setattr(serve, "tls", lambda cfg=None: (8443, *cert))
    r = client.get("/api/v1/images?x=1", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "https://testserver:8443/api/v1/images?x=1"
    assert client.get("/", follow_redirects=False).status_code == 308
    assert client.get("/api/health").status_code == 200                      # the PCs' paths
    assert client.get("/api/v1/boot/ipxe?mac=aa:bb:cc:dd:ee:ff", follow_redirects=False).status_code != 308
    assert client.get("/api/v1/agent/package", follow_redirects=False).status_code != 308
    r = client.get("https://testserver/api/v1/images", follow_redirects=False)  # already HTTPS
    assert r.status_code == 200


def test_settings_show_the_certificate_and_offer_it(client, cert, monkeypatch):
    assert client.get("/api/v1/settings/tls").json()["enabled"] is False
    monkeypatch.setattr(serve, "tls", lambda cfg=None: (8443, *cert))
    info = client.get("https://testserver/api/v1/settings/tls").json()
    assert info["enabled"] and info["self_signed"]
    assert set(info["names"]) == {"192.168.0.40", "pve"}
    assert len(info["sha256"].split(":")) == 32
    r = client.get("https://testserver/api/v1/settings/tls/certificate")
    assert r.status_code == 200 and r.content == cert[0].read_bytes()


def test_the_session_cookie_is_secure_once_https_is_set_up(anon_client, cert, monkeypatch):
    from app.api.v1 import auth

    class Req:
        class url:
            scheme = "http"

    assert auth._secure(Req) is False
    monkeypatch.setattr(serve, "tls", lambda cfg=None: (8443, *cert))
    assert auth._secure(Req) is True
