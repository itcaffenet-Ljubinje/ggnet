"""Settings → Network boot: the boot settings and the PXE service (FakeRunner)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.api.deps import get_pxe_service
from app.db.session import make_engine
from app.main import app
from app.services.pxe import PxeService, effective_config, load_settings

from .conftest import FakeRunner

SHOW = "LoadState=loaded\nActiveState=active\nSubState=running\nActiveEnterTimestamp=@1792297503"


CERT = ("subject=CN = ggNet Secure Boot, O = IT Caffe Net\nnotAfter=Oct 16 00:33:03 2036 GMT\n"
        "sha256 Fingerprint=FA:23:CA:52:6C:3E")


@pytest.fixture
def pxe(client, tmp_path):
    runner = FakeRunner()
    runner.on("systemctl", "show", result=(True, SHOW, ""))
    runner.on("openssl", "x509", result=(True, CERT, ""))
    (tmp_path / "snponly.efi").write_bytes(b"x")
    (tmp_path / "autoexec.ipxe").write_text("#!ipxe\n")
    service = PxeService("ggnet-pxe", tmp_path, runner, secureboot_dir=tmp_path / "secureboot")
    app.dependency_overrides[get_pxe_service] = lambda: service
    return runner


def test_boot_settings_default_to_config_toml(client):
    r = client.get("/api/v1/settings/boot")
    assert r.status_code == 200
    assert r.json() == {"gateway": "none", "unregistered": "wait", "mok_enroll": False,
                        "mok_password": "ggnet", "new_pc_name": "pc-{nn}", "network_boot": True,
                        "saved": False}


def test_boot_settings_are_saved(client):
    r = client.put("/api/v1/settings/boot", json={"gateway": " 192.168.0.1 ", "unregistered": "local"})
    assert r.status_code == 200, r.text
    assert r.json() == {"gateway": "192.168.0.1", "unregistered": "local", "mok_enroll": False,
                        "mok_password": "ggnet", "new_pc_name": "pc-{nn}", "network_boot": True,
                        "saved": True}
    assert client.get("/api/v1/settings/boot").json()["saved"] is True


@pytest.mark.parametrize("body", [
    {"gateway": "router", "unregistered": "wait"},
    {"gateway": "192.168.0.300", "unregistered": "wait"},
    {"gateway": "none", "unregistered": "never"},
    {"gateway": "none", "mok_password": "abc"},          # too short
    {"gateway": "none", "mok_password": "GGnet!"},       # MokManager's keyboard is US: a-z, 0-9
])
def test_invalid_boot_settings_are_refused(client, body):
    assert client.put("/api/v1/settings/boot", json=body).status_code == 422
    assert client.get("/api/v1/settings/boot").json()["saved"] is False


def test_effective_config_keeps_config_toml_until_saved(client, db_url):
    cfg = {"boot": {"gateway": "dhcp", "pxe_service": "x"}, "paths": {}}
    engine = make_engine(db_url)
    with Session(engine) as db:
        assert effective_config(db, cfg) is cfg
        s, saved = load_settings(db, cfg)
        assert (s.gateway, s.unregistered, saved) == ("dhcp", "wait", False)
        client.put("/api/v1/settings/boot", json={"gateway": "none", "unregistered": "local"})
        merged = effective_config(db, cfg)
        assert merged["boot"] == {"gateway": "none", "unregistered": "local", "pxe_service": "x",
                                  "mok_enroll": False, "mok_password": "ggnet",
                                  "new_pc_name": "pc-{nn}", "network_boot": True}
    engine.dispose()


def test_pxe_status(client, pxe, tmp_path):
    r = client.get("/api/v1/pxe")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit"] == "ggnet-pxe" and body["installed"] is True
    assert (body["state"], body["sub_state"]) == ("active", "running")
    assert datetime.fromisoformat(body["since"]) == datetime.fromtimestamp(1792297503, tz=timezone.utc)
    assert body["tftp_root"] == str(tmp_path)
    assert body["files"] == {"snponly.efi": True, "ipxe.efi": False, "autoexec.ipxe": True,
                             "sb/shimx64.efi": False, "sb/mmx64.efi": False, "sb/grubx64.efi": False}
    assert body["secure_boot"] is None          # no key made yet
    assert pxe.calls[0][:3] == ["systemctl", "show", "ggnet-pxe"]


def test_pxe_status_of_a_missing_unit(client, pxe):
    pxe.on("systemctl", "show", result=(
        True, "LoadState=not-found\nActiveState=inactive\nSubState=dead\nActiveEnterTimestamp=", ""))
    body = client.get("/api/v1/pxe").json()
    assert body["installed"] is False and body["state"] == "inactive" and body["since"] is None


def test_pxe_restart(client, pxe):
    r = client.post("/api/v1/pxe/restart")
    assert r.status_code == 200, r.text
    assert ["systemctl", "restart", "ggnet-pxe"] in pxe.calls
    assert r.json()["state"] == "active"


def test_pxe_restart_failure_is_reported(client, pxe):
    pxe.on("systemctl", "restart", result=(False, "", "Job for ggnet-pxe.service failed"))
    r = client.post("/api/v1/pxe/restart")
    assert r.status_code == 502
    assert "Job for ggnet-pxe.service failed" in r.text


def test_pxe_service_from_config():
    s = PxeService.from_config({"paths": {"data_dir": "/var/lib/ggnet"}, "boot": {"pxe_service": "ggnet-dev-dnsmasq"}})
    assert (s.unit, str(s.tftp_root)) == ("ggnet-dev-dnsmasq", "/var/lib/ggnet/tftp")
    s = PxeService.from_config({"paths": {"data_dir": "/x"}, "services": {"tftp_root": "/srv/tftp"}})
    assert (s.unit, str(s.tftp_root)) == ("ggnet-pxe", "/srv/tftp")


def _secure_boot(tmp_path, ipxe: bool = True) -> None:
    """What the installer leaves: the key's certificate and the signed iPXE for shim."""
    sb = tmp_path / "secureboot"
    sb.mkdir()
    (sb / "ggnet-sb.cer").write_bytes(b"0\x82DER")
    (sb / "ggnet-sb.crt").write_text("-----BEGIN CERTIFICATE-----\n")
    (tmp_path / "sb").mkdir()
    for name in ("shimx64.efi", "mmx64.efi") + (("grubx64.efi",) if ipxe else ()):
        (tmp_path / "sb" / name).write_bytes(b"MZ")


def test_pxe_status_shows_the_secure_boot_key(client, pxe, tmp_path):
    _secure_boot(tmp_path)
    body = client.get("/api/v1/pxe").json()
    assert body["files"]["sb/shimx64.efi"] and body["files"]["sb/grubx64.efi"]
    assert body["secure_boot"] == {
        "subject": "CN = ggNet Secure Boot, O = IT Caffe Net",
        "fingerprint": "FA:23:CA:52:6C:3E",
        "expires": "2036-10-16T00:33:03Z",
        "ipxe_signed": True,
    }
    assert ["sbverify", "--cert", str(tmp_path / "secureboot" / "ggnet-sb.crt"),
            str(tmp_path / "sb" / "grubx64.efi")] in pxe.calls


def test_an_unsigned_or_missing_ipxe_is_shown(client, pxe, tmp_path):
    _secure_boot(tmp_path)
    pxe.on("sbverify", result=(False, "", "Signature verification failed"))
    assert client.get("/api/v1/pxe").json()["secure_boot"]["ipxe_signed"] is False
    (tmp_path / "sb" / "grubx64.efi").unlink()
    assert client.get("/api/v1/pxe").json()["secure_boot"]["ipxe_signed"] is None


def test_the_certificate_is_downloaded_without_login(client, pxe, tmp_path):
    assert client.get("/api/v1/boot/secureboot.cer").status_code == 404
    _secure_boot(tmp_path)
    r = client.get("/api/v1/boot/secureboot.cer")
    assert r.status_code == 200
    assert r.content == b"0\x82DER"
    assert r.headers["content-type"] == "application/pkix-cert"
    assert "ggnet-sb.cer" in r.headers["content-disposition"]


def test_secureboot_dir_defaults_next_to_config_toml(monkeypatch):
    monkeypatch.setenv("GGNET_CONFIG", "/var/lib/ggnet-dev/config.toml")
    assert PxeService.from_config({"paths": {"data_dir": "/x"}}).secureboot_dir == Path("/var/lib/ggnet-dev/secureboot")
    s = PxeService.from_config({"paths": {"data_dir": "/x"}, "boot": {"secureboot_dir": "/srv/sb"}})
    assert s.secureboot_dir == Path("/srv/sb")


# ── Secure Boot key enrollment by the agent ──────────────────────────

def _beat(client, name: str, **extra) -> dict:
    r = client.post("/api/v1/agent/heartbeat", json={"name": name, "agent_version": "0.1.13", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_boot_mode_agents_get_the_key_only_while_enrollment_is_on(client, pxe, tmp_path):
    _secure_boot(tmp_path)
    client.post("/api/v1/machines", json={"name": "pc01", "mode": "boot", "mac": "aa:bb:cc:dd:ee:01"})
    client.post("/api/v1/machines", json={"name": "pc02", "mac": "aa:bb:cc:dd:ee:02"})   # Disk Mode
    assert _beat(client, "pc01")["secure_boot"] is None          # off by default
    r = client.put("/api/v1/settings/boot", json={"mok_enroll": True, "mok_password": "cafe2026"})
    assert r.status_code == 200, r.text
    assert _beat(client, "pc01")["secure_boot"] == {
        "certificate": base64.b64encode(b"0\x82DER").decode(), "password": "cafe2026"}
    assert _beat(client, "pc02")["secure_boot"] is None          # boots its own Windows


def test_no_key_on_the_server_means_nothing_to_enroll(client, pxe):
    client.post("/api/v1/machines", json={"name": "pc01", "mode": "boot", "mac": "aa:bb:cc:dd:ee:01"})
    client.put("/api/v1/settings/boot", json={"mok_enroll": True})
    assert _beat(client, "pc01")["secure_boot"] is None


def test_the_agent_reports_the_key_state(client):
    m = client.post("/api/v1/machines", json={"name": "pc01", "mode": "boot"}).json()
    _beat(client, "pc01", mok="requested")
    assert client.get(f"/api/v1/machines/{m['id']}").json()["mok_state"] == "requested"
    _beat(client, "pc01")                                        # nothing new: kept
    assert client.get(f"/api/v1/machines/{m['id']}").json()["mok_state"] == "requested"
    _beat(client, "pc01", mok="enrolled")
    assert client.get(f"/api/v1/machines/{m['id']}").json()["mok_state"] == "enrolled"
    r = client.post("/api/v1/agent/heartbeat", json={"name": "pc01", "agent_version": "0.1.13", "mok": "x"})
    assert r.status_code == 422


# ── Signing wimboot for Secure Boot ───────────────────────────────────

def _signer(tmp_path, with_key: bool = True):
    from app.services.pxe import PxeService

    from .conftest import FakeRunner

    sb = tmp_path / "secureboot"
    sb.mkdir()
    if with_key:
        (sb / "ggnet-sb.key").write_text("key")
        (sb / "ggnet-sb.crt").write_text("cert")
    runner = FakeRunner()

    def sign(cmd):
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_bytes(Path(cmd[-1]).read_bytes() + b"+signed")
        return True, "", ""
    runner.on("sbsign", result=sign)
    src = tmp_path / "winpe" / "wimboot"
    src.parent.mkdir()
    src.write_bytes(b"MZwimboot")
    return PxeService("ggnet-pxe", tmp_path / "tftp", runner=runner, secureboot_dir=sb), runner, src


def test_wimboot_is_signed_once_and_again_when_it_changes(tmp_path):
    import os

    service, runner, src = _signer(tmp_path)
    dest = tmp_path / "data" / "winpe-signed" / "wimboot"
    assert service.signed_copy(src, dest) == dest
    assert dest.read_bytes() == b"MZwimboot+signed"
    assert any(c[0] == "sbverify" for c in runner.calls)
    runner.calls.clear()
    assert service.signed_copy(src, dest) == dest
    assert runner.calls == []                                        # kept
    os.utime(src, (dest.stat().st_mtime + 10, dest.stat().st_mtime + 10))
    assert service.signed_copy(src, dest) == dest
    assert any(c[0] == "sbsign" for c in runner.calls)               # a new wimboot: signed again


def test_without_the_key_or_when_signing_fails_wimboot_is_served_as_it_is(tmp_path):
    service, runner, src = _signer(tmp_path, with_key=False)
    dest = tmp_path / "out" / "wimboot"
    assert service.signed_copy(src, dest) == src and runner.calls == []
    other = tmp_path / "b"
    other.mkdir()
    service, runner, src = _signer(other)
    runner.on("sbverify", result=(False, "", "Signature verification failed"))
    assert service.signed_copy(src, other / "out" / "wimboot") == src
    assert not (other / "out" / "wimboot.new").exists()
