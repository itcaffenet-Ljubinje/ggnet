"""Setting up the hardware scan's Windows PE from the web UI (app.services.winpe, app.api.v1.winpe)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.api import deps
from app.api.v1 import boot
from app.api.v1 import winpe as winpe_api
from app.main import app
from app.services import scan, winpe

INF = r"""
; Intel's e1d.inf, cut down
[Version]
Signature   = "$Windows NT$"
Class       = Net
ClassGUID   = {4d36e972-e325-11ce-bfc1-08002be10318}
Provider    = %Intel%
CatalogFile = e1d.cat     ; signed

[Manufacturer]
%Intel%     = Intel, NTamd64.10.0...19041, NTx86

[Intel.NTamd64.10.0...19041]
%E15BCNC.DeviceDesc%  = E15BC.10.0.1, PCI\VEN_8086&DEV_15BC
%E15BCNC.DeviceDesc%  = E15BC.10.0.1, PCI\VEN_8086&DEV_15BC&SUBSYS_00008086
%E0D4FNC.DeviceDesc%  = E0D4F.10.0.1, PCI\VEN_8086&DEV_0D4F

[Intel.NTx86]
%E1533NC.DeviceDesc%  = E1533, PCI\VEN_8086&DEV_1533

[Strings]
Intel = "Intel"
E15BCNC.DeviceDesc = "Intel(R) Ethernet Connection (7)  I219-V"
E0D4FNC.DeviceDesc = "Intel(R) Ethernet Connection (10) I219-V ; not a comment"
"""


def _package(folder: Path, inf: str = INF, files=("e1d.sys", "e1d.cat")) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "e1d.inf").write_text(inf)
    for name in files:
        (folder / name).write_bytes(b"x")
    return folder


# ── Driver packages ───────────────────────────────────────────────────

def test_an_inf_tells_its_class_catalog_and_64_bit_devices():
    info = winpe.read_inf("e1d.inf", INF)
    assert info.network and info.x64 and info.catalog == "e1d.cat"
    assert info.devices == {"8086-15bc": "Intel(R) Ethernet Connection (7) I219-V",
                            "8086-0d4f": "Intel(R) Ethernet Connection (10) I219-V ; not a comment"}
    # The 32-bit section only: no device for 64-bit Windows.
    x86 = INF.replace("NTamd64.10.0...19041, NTx86", "NTx86")
    assert not winpe.read_inf("e1d.inf", x86).x64
    # An undecorated models section is for every platform.
    plain = INF.replace("Intel, NTamd64.10.0...19041, NTx86", "Intel.NTamd64.10.0...19041")
    assert winpe.read_inf("e1d.inf", plain).devices


def test_a_package_must_be_a_signed_64_bit_network_driver(tmp_path):
    winpe.check_package(winpe.package_info(_package(tmp_path / "ok")))
    with pytest.raises(winpe.WinPeError, match="no .inf"):
        winpe.check_package(winpe.PackageInfo("noinf", ["e1d.sys"], 1, []))
    with pytest.raises(winpe.WinPeError, match="64-bit"):
        winpe.check_package(winpe.package_info(_package(tmp_path / "x86", INF.replace(
            "NTamd64.10.0...19041, NTx86", "NTx86"))))
    with pytest.raises(winpe.WinPeError, match="64-bit"):
        winpe.check_package(winpe.package_info(_package(tmp_path / "usb", INF.replace("Class       = Net", "Class = USB")
                                                        .replace("{4d36e972", "{36fc9e60"))))
    with pytest.raises(winpe.WinPeError, match="e1d.cat, which is not in the folder"):
        winpe.check_package(winpe.package_info(_package(tmp_path / "nocat", files=("e1d.sys",))))


def test_a_package_is_uploaded_file_by_file_and_added_when_complete(tmp_path):
    library = scan.DriverLibrary(tmp_path / "drivers")
    uploads = winpe.DriverUploads(library)
    uploads.start("intel-pro1000")
    for name in ("e1d.inf", "e1d.sys"):
        uploads.write("intel-pro1000", name, [INF.encode() if name.endswith(".inf") else b"MZ"])
    assert library.packages() == []                          # not while it is being uploaded
    with pytest.raises(winpe.WinPeError, match="e1d.cat"):
        uploads.finish("intel-pro1000")                      # the catalog is missing
    assert not (library.root / ".intel-pro1000.upload").exists()

    uploads.start("intel-pro1000")
    for name, data in (("e1d.inf", INF.encode()), ("e1d.sys", b"MZ"), ("e1d.cat", b"cat")):
        uploads.write("intel-pro1000", name, [data])
    info = uploads.finish("intel-pro1000")
    assert info.files == ["e1d.cat", "e1d.inf", "e1d.sys"]
    assert library.match("8086-15bc") == ("intel-pro1000", ["e1d.inf"])
    with pytest.raises(winpe.WinPeError, match="already has"):
        uploads.start("intel-pro1000")
    with pytest.raises(winpe.WinPeError, match="lower-case"):
        uploads.start("../etc")
    uploads.start("other")
    with pytest.raises(winpe.WinPeError, match="driver file names"):
        uploads.write("other", "../x.inf", [b""])
    assert uploads.remove("intel-pro1000") and not uploads.remove("intel-pro1000")
    assert library.packages() == []


# ── Windows PE from an ISO ────────────────────────────────────────────

class IsoRunner:
    """mount puts Windows installation media into the target folder; umount empties it."""

    def __init__(self, media: Path | None):
        self.media, self.calls = media, []

    def run(self, cmd, timeout=None, **kw):
        self.calls.append(cmd[0])
        if cmd[0] == "mount":
            if self.media is None:
                return False, "", "mount: wrong fs type"
            shutil.copytree(self.media, cmd[-1], dirs_exist_ok=True)
        elif cmd[0] == "umount":
            for p in Path(cmd[-1]).iterdir():
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        return True, "", ""


def _media(root: Path, x64: bool = True) -> Path:
    for rel in ("Boot/BCD", "boot/boot.sdi", "sources/boot.wim", "efi/microsoft/boot/bcd")\
            + (("efi/boot/bootx64.efi",) if x64 else ()):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(rel.encode())
    return root


def test_windows_pe_is_copied_out_of_a_windows_iso(tmp_path):
    folder = tmp_path / "winpe"
    folder.mkdir()
    (folder / "bcd").write_bytes(b"old")
    (folder / "wimboot").write_bytes(b"wimboot")
    runner = IsoRunner(_media(tmp_path / "media"))
    setup = winpe.WinPeSetup(runner, folder, mounts=tmp_path / "mnt")
    assert setup.from_iso(tmp_path / "Windows11.iso") == ["BCD", "boot.sdi", "boot.wim"]
    assert runner.calls == ["mount", "umount"]
    assert sorted(p.name for p in folder.iterdir()) == ["BCD", "boot.sdi", "boot.wim", "wimboot"]
    assert (folder / "BCD").read_bytes() == b"Boot/BCD"                  # boot\bcd first
    assert scan.winpe_ready(folder)


def test_an_iso_that_is_not_64_bit_windows_media_is_refused(tmp_path):
    folder = tmp_path / "winpe"
    runner = IsoRunner(_media(tmp_path / "media", x64=False))
    with pytest.raises(winpe.WinPeError, match="64-bit"):
        winpe.WinPeSetup(runner, folder, mounts=tmp_path / "mnt").from_iso(tmp_path / "arm.iso")
    assert runner.calls == ["mount", "umount"]                            # unmounted all the same
    (tmp_path / "empty").mkdir()
    with pytest.raises(winpe.WinPeError, match="Windows installation media"):
        winpe.WinPeSetup(IsoRunner(tmp_path / "empty"), folder, mounts=tmp_path / "mnt2").from_iso(tmp_path / "x.iso")
    with pytest.raises(winpe.WinPeError, match="cannot open"):
        winpe.WinPeSetup(IsoRunner(None), folder, mounts=tmp_path / "mnt3").from_iso(tmp_path / "x.iso")


def test_isos_are_listed_from_the_iso_folders(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "Win11_24H2.ISO").write_bytes(b"iso")
    (tmp_path / "a" / ".hidden.iso").write_bytes(b"iso")
    (tmp_path / "a" / "disk.vhdx").write_bytes(b"x")
    found = winpe.list_isos([tmp_path / "a", tmp_path / "missing"])
    assert [(f.name, f.size) for f in found] == [("Win11_24H2.ISO", 3)]


# ── API ───────────────────────────────────────────────────────────────

@pytest.fixture
def setup(client, tmp_path, monkeypatch):
    folder = tmp_path / "winpe"
    folder.mkdir()
    (folder / "wimboot").write_bytes(b"wimboot")
    scanner = tmp_path / "ggnet-scan.exe"
    scanner.write_bytes(b"MZ")
    isos = tmp_path / "iso"
    isos.mkdir()
    (isos / "Windows11.iso").write_bytes(b"iso")
    library = scan.DriverLibrary(tmp_path / "drivers")
    monkeypatch.setattr(scan, "winpe_dir", lambda cfg: folder)
    monkeypatch.setattr(boot, "_scanner", lambda: scanner)
    runner = IsoRunner(_media(tmp_path / "media"))
    app.dependency_overrides[deps.get_driver_library] = lambda: library
    app.dependency_overrides[winpe_api.get_iso_folders] = lambda: [isos]
    app.dependency_overrides[winpe_api.get_winpe_setup] = lambda: winpe.WinPeSetup(runner, folder, tmp_path / "mnt")
    return isos / "Windows11.iso"


def test_the_ui_sets_up_windows_pe_and_its_drivers(client, setup, db_url):
    out = client.get("/api/v1/winpe").json()
    assert not out["ready"] and out["scanner"]
    assert [(f["name"], f["present"]) for f in out["files"]] == [
        ("wimboot", True), ("BCD", False), ("boot.sdi", False), ("boot.wim", False)]
    assert [i["name"] for i in out["isos"]] == ["Windows11.iso"]

    assert client.post("/api/v1/winpe/from-iso", json={"path": "/etc/passwd"}).status_code == 404
    r = client.post("/api/v1/winpe/from-iso", json={"path": str(setup)})
    assert r.status_code == 200, r.text
    assert r.json()["ready"]

    assert client.post("/api/v1/winpe/drivers/intel-pro1000/upload").status_code == 204
    for name, data in (("e1d.inf", INF.encode()), ("e1d.sys", b"MZ"), ("e1d.cat", b"cat")):
        assert client.put(f"/api/v1/winpe/drivers/intel-pro1000/files/{name}", content=data).status_code == 204
    r = client.post("/api/v1/winpe/drivers/intel-pro1000")
    assert r.status_code == 201, r.text
    assert r.json()["devices"][0] == {"chip": "8086-0d4f", "vendor": "Intel",
                                      "name": "Intel(R) Ethernet Connection (10) I219-V ; not a comment"}

    # The PCs' chips: one has a driver in the library, one not.
    from sqlalchemy.orm import Session

    from app.db.models import Machine
    from app.db.session import make_engine
    engine = make_engine(db_url)
    with Session(engine) as db:
        for i, nic in enumerate(("VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10", "VEN_10EC&DEV_8125&SUBSYS_86771043&REV_05",
                                 "VEN_10EC&DEV_8125&SUBSYS_86771043&REV_04")):
            db.add(Machine(name=f"pc-{i}", initiator_iqn=f"iqn.1991-05.com.microsoft:pc-{i}",
                           mac=f"00:11:22:33:44:5{i}", boot_nic=nic))
        db.add(Machine(name="pc-x", initiator_iqn="iqn.1991-05.com.microsoft:pc-x"))
        db.commit()
    engine.dispose()
    chips = client.get("/api/v1/winpe").json()["pc_chips"]
    assert chips == [
        {"chip": "10ec-8125", "vendor": "Realtek", "name": None, "package": None, "pcs": 2},
        {"chip": "8086-15bc", "vendor": "Intel", "name": "Intel(R) Ethernet Connection (7) I219-V",
         "package": "intel-pro1000", "pcs": 1},
    ]

    assert client.post("/api/v1/winpe/drivers/bad/upload").status_code == 204
    client.put("/api/v1/winpe/drivers/bad/files/readme.txt", content=b"hi")
    r = client.post("/api/v1/winpe/drivers/bad")
    assert r.status_code == 409 and "no .inf" in r.text
    assert client.delete("/api/v1/winpe/drivers/intel-pro1000").status_code == 204
    assert client.delete("/api/v1/winpe/drivers/intel-pro1000").status_code == 404
