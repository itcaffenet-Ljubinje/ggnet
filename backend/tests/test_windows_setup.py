"""Setting up a Boot Mode PC's OS writeback as that PC (app.windows_setup)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.windows_setup import (
    OsSetup,
    WindowsSetup,
    nic_known,
    registry_patch,
    reserved_letters,
    static_interfaces,
)

from .conftest import FakeRunner

SETUP = OsSetup("vip-05", {"GGNET_SERVER": "http://192.168.0.40:8088", "GGNET_MACHINE_ID": "7"})
DEVICE = "/dev/zvol/tank/ggnet/writebacks/vip-05-os"


def _lsblk(*parts: tuple[str, str | None, int]) -> str:
    children = [{"path": p, "type": "part", "fstype": fs, "size": size} for p, fs, size in parts]
    return json.dumps({"blockdevices": [{"path": "/dev/zd16", "type": "disk", "fstype": None,
                                         "size": 100 << 30, "children": children}]})


STANDARD_DISK = _lsblk(
    ("/dev/zd16p1", "vfat", 100 << 20),        # EFI
    ("/dev/zd16p2", None, 16 << 20),           # MSR
    ("/dev/zd16p3", "ntfs", 99 << 30),         # Windows
    ("/dev/zd16p4", "ntfs", 700 << 20),        # Recovery
)


@pytest.fixture
def runner() -> FakeRunner:
    r = FakeRunner()
    r.responses[("lsblk",)] = (True, STANDARD_DISK, "")
    r.responses[("hivexregedit", "--export")] = (
        True, 'Windows Registry Editor Version 5.00\r\n\r\n[\\Select]\r\n"Current"=dword:00000002\r\n', "")
    return r


@pytest.fixture
def windows(runner, tmp_path) -> WindowsSetup:
    return WindowsSetup(runner, run_dir=tmp_path / "run")


def _windows_on(windows: WindowsSetup, name: str = "vip-05") -> Path:
    """What ntfs-3g would show: a Windows tree in the mount directory (names as Windows has them)."""
    config = windows.run_dir / name / "Windows" / "System32" / "config"
    config.mkdir(parents=True)
    (config / "SYSTEM").write_bytes(b"regf")
    return config / "SYSTEM"


def _cmds(runner: FakeRunner) -> list[list[str]]:
    """What was done, without udev waits and file system probes (blkid -p)."""
    return [c for c in runner.calls if c[0] not in ("udevadm", "blkid")]


# ── The registry patch ───────────────────────────────────────────────

def test_registry_patch():
    assert registry_patch(SETUP, 2) == (
        "Windows Registry Editor Version 5.00\n"
        "\n"
        "[\\ControlSet002\\Control\\ComputerName\\ComputerName]\n"
        '"ComputerName"="VIP-05"\n'
        "\n"
        "[\\ControlSet002\\Services\\Tcpip\\Parameters]\n"
        '"Hostname"="vip-05"\n'
        '"NV Hostname"="vip-05"\n'
        "\n"
        "[\\ControlSet002\\Control\\Session Manager\\Environment]\n"
        '"GGNET_SERVER"="http://192.168.0.40:8088"\n'
        '"GGNET_MACHINE_ID"="7"\n'
        "\n"
        "[\\ControlSet002\\Control\\BitLocker]\n"
        '"PreventDeviceEncryption"=dword:00000001\n'
    )


def test_registry_patch_escapes_and_refuses_bad_input():
    patch = registry_patch(OsSetup("pc01", {"X": 'C:\\Games "x"'}), 1)
    assert '"X"="C:\\\\Games \\"x\\""' in patch
    for bad in (OsSetup("PC 01"), OsSetup("pc01", {"A-B": "1"}), OsSetup("pc01", {"A": "1\n[\\x]"})):
        with pytest.raises(ValueError):
            registry_patch(bad, 1)


# ── personalize() ────────────────────────────────────────────────────

def test_personalize_merges_into_the_windows_partition(windows, runner):
    hive = _windows_on(windows)
    merged = {}

    def merge(cmd):
        merged["reg"] = Path(cmd[3]).read_text(encoding="utf-8")
        return True, "", ""
    runner.responses[("hivexregedit", "--merge")] = merge
    mount = str(windows.run_dir / "vip-05")

    assert windows.personalize(DEVICE, SETUP) is None
    assert _cmds(runner) == [
        ["lsblk", "-J", "-b", "-p", "-o", "PATH,TYPE,FSTYPE,SIZE", DEVICE],
        ["lsblk", "-J", "-b", "-p", "-o", "PATH,TYPE,FSTYPE,SIZE", DEVICE],     # BitLocker?
        ["ntfs-3g", "-o", "rw,noatime", "/dev/zd16p3", mount],   # the big NTFS partition
        ["hivexregedit", "--export", str(hive), "\\Select"],
        ["hivexregedit", "--export", str(hive),
         "\\ControlSet002\\Services\\Tcpip\\Parameters\\Interfaces"],
        ["hivexregedit", "--export", str(hive), "\\MountedDevices"],
        ["hivexregedit", "--merge", str(hive), str(windows.run_dir / "vip-05.reg")],
        ["umount", mount],
    ]
    assert merged["reg"] == registry_patch(SETUP, 2)   # control set from \Select\Current
    assert not (windows.run_dir / "vip-05.reg").exists()


def test_personalize_finds_the_hive_whatever_its_case(windows, runner):
    config = windows.run_dir / "vip-05" / "WINDOWS" / "system32" / "Config"
    config.mkdir(parents=True)
    (config / "system").write_bytes(b"regf")
    assert windows.personalize(DEVICE, SETUP) is None
    assert ["hivexregedit", "--merge", str(config / "system"),
            str(windows.run_dir / "vip-05.reg")] in runner.calls


def test_control_set_defaults_to_1(windows, runner):
    runner.responses[("hivexregedit", "--export")] = (False, "", "no such key")
    _windows_on(windows)
    merged = {}

    def merge(cmd):
        merged["reg"] = Path(cmd[3]).read_text(encoding="utf-8")
        return True, "", ""
    runner.responses[("hivexregedit", "--merge")] = merge
    assert windows.personalize(DEVICE, SETUP) is None
    assert "[\\ControlSet001\\Control\\ComputerName\\ComputerName]" in merged["reg"]


def test_an_image_without_windows(windows, runner):
    runner.responses[("lsblk",)] = (True, _lsblk(), "")
    assert windows.personalize(DEVICE, SETUP) == f"no Windows partition found on {DEVICE}"
    assert not any(c[0] == "ntfs-3g" for c in runner.calls)


def test_tries_the_next_partition_and_reports_each(windows, runner):
    runner.responses[("ntfs-3g", "-o", "rw,noatime", "/dev/zd16p3")] = (
        False, "", "Windows is hibernated, refused to mount.")
    problem = windows.personalize(DEVICE, SETUP)
    assert problem == ("/dev/zd16p3: Windows is hibernated, refused to mount.; "
                       "/dev/zd16p4: no Windows on it")
    # Only the partition that was mounted is unmounted.
    assert [c for c in runner.calls if c[0] == "umount"] == [["umount", str(windows.run_dir / "vip-05")]]
    assert not any(c[:2] == ["hivexregedit", "--merge"] for c in runner.calls)


def test_a_failed_merge_is_reported_and_unmounted(windows, runner):
    _windows_on(windows)
    runner.responses[("hivexregedit", "--merge")] = (False, "", "hive is corrupt")
    assert windows.personalize(DEVICE, SETUP) == "editing the registry failed: hive is corrupt"
    assert runner.calls[-1] == ["umount", str(windows.run_dir / "vip-05")]


def test_bad_input_never_touches_the_host(windows, runner):
    assert "Invalid computer name" in windows.personalize(DEVICE, OsSetup("Bad Name"))
    assert runner.calls == []


def test_unreadable_lsblk_counts_as_no_windows(windows, runner):
    runner.responses[("lsblk",)] = (True, "not json", "")
    assert windows.personalize(DEVICE, SETUP).startswith("no Windows partition")


def test_an_unexpected_error_is_returned_not_raised(windows, runner):
    def boom(cmd):
        raise RuntimeError("disk vanished")
    runner.responses[("lsblk",)] = boom
    assert windows.personalize(DEVICE, SETUP) == "unexpected error: disk vanished"


def test_partitions_listed_next_to_the_disk(windows, runner):
    """lsblk on PVE 9 lists the partitions of a disk named on its command line flat."""
    runner.responses[("lsblk",)] = (True, json.dumps({"blockdevices": [
        {"path": "/dev/zd64", "type": "disk", "fstype": None, "size": 161061273600},
        {"path": "/dev/zd64p1", "type": "part", "fstype": None, "size": 16759808},
        {"path": "/dev/zd64p2", "type": "part", "fstype": "ntfs", "size": 161042399232},
    ]}), "")
    assert windows.ntfs_partitions(DEVICE) == ["/dev/zd64p2"]


# ── Fixed IPv4 addresses become DHCP ─────────────────────────────────

def _multi_sz(*items: str) -> str:
    raw = ("\0".join(items) + "\0\0").encode("utf-16-le")
    return "hex(7):" + ",".join(f"{b:02x}" for b in raw)


FIXED = "{ce61fd57-992d-4f05-86b6-c4d2b5b7ea91}"
DHCP = "{0b0c1e05-dc82-44aa-ab40-16c8ae66f8a0}"
UNSET = "{11111111-2222-3333-4444-555555555555}"
INTERFACES = (
    "Windows Registry Editor Version 5.00\r\n\r\n"
    "[\\ControlSet002\\Services\\Tcpip\\Parameters\\Interfaces]\r\n\r\n"
    f"[\\ControlSet002\\Services\\Tcpip\\Parameters\\Interfaces\\{DHCP}]\r\n"
    '"EnableDHCP"=dword:00000001\r\n\r\n'
    # vip-05's NIC as Disk2vhd captured it: a fixed address, wrapped like hivexregedit does
    f"[\\ControlSet002\\Services\\Tcpip\\Parameters\\Interfaces\\{FIXED}]\r\n"
    f'"DefaultGateway"={_multi_sz("192.168.0.254")}\r\n'
    '"EnableDHCP"=dword:00000000\r\n'
    f'"IPAddress"={_multi_sz("192.168.0.25")[:40]}\\\r\n  {_multi_sz("192.168.0.25")[40:]}\r\n'
    f'"SubnetMask"={_multi_sz("255.255.255.0")}\r\n\r\n'
    f"[\\ControlSet002\\Services\\Tcpip\\Parameters\\Interfaces\\{FIXED}\\{{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}}]\r\n"
    '"EnableDHCP"=dword:00000000\r\n\r\n'
    f"[\\ControlSet002\\Services\\Tcpip\\Parameters\\Interfaces\\{UNSET}]\r\n"
    '"EnableDHCP"=dword:00000000\r\n'
    f'"IPAddress"={_multi_sz("0.0.0.0")}\r\n'
)


def test_static_interfaces_finds_only_fixed_addresses():
    assert static_interfaces(INTERFACES) == [FIXED]
    assert static_interfaces("") == []


# Every patch ends with this (no_device_encryption_lines).
NO_ENCRYPTION = '\n[\\ControlSet002\\Control\\BitLocker]\n"PreventDeviceEncryption"=dword:00000001\n'


def test_registry_patch_switches_interfaces_to_dhcp():
    patch = registry_patch(SETUP, 2, [FIXED]).removesuffix(NO_ENCRYPTION)
    assert patch.endswith(
        f"[\\ControlSet002\\Services\\Tcpip\\Parameters\\Interfaces\\{FIXED}]\n"
        '"EnableDHCP"=dword:00000001\n'
        '"IPAddress"=-\n"SubnetMask"=-\n"DefaultGateway"=-\n"DefaultGatewayMetric"=-\n'
    )


def test_personalize_turns_a_fixed_address_into_dhcp(windows, runner):
    _windows_on(windows)
    select = runner.responses[("hivexregedit", "--export")]
    merged = {}

    def export(cmd):
        return (True, INTERFACES, "") if cmd[-1].endswith("\\Interfaces") else select

    def merge(cmd):
        merged["reg"] = Path(cmd[3]).read_text(encoding="utf-8")
        return True, "", ""
    runner.responses[("hivexregedit", "--export")] = export
    runner.responses[("hivexregedit", "--merge")] = merge
    assert windows.personalize(DEVICE, SETUP) is None
    assert merged["reg"] == registry_patch(SETUP, 2, [FIXED])


# ── Drive letters of the PC the image was made on are forgotten ──────

MOUNTED = (
    "Windows Registry Editor Version 5.00\r\n\r\n"
    "[\\MountedDevices]\r\n"
    '"\\\\??\\\\Volume{6f0a2e2c-1111-2222-3333-444455556666}"=hex(3):44,4d,49,4f\r\n'
    '"\\\\DosDevices\\\\C:"=hex(3):44,4d,49,4f,3a,49,44,3a,42,5c\r\n'
    '"\\\\DosDevices\\\\D:"=hex(3):44,4d,49,4f,3a,49,44,3a,30,25\r\n'
    '"\\\\DosDevices\\\\F:"=hex(3):44,4d,49,4f,3a,49,44,3a,6e,fb\r\n'
    '"\\\\DosDevices\\\\E:"=hex(3):44,4d,49,4f,3a,49,44,3a,54,b8\r\n'
)


def test_reserved_letters_keeps_c():
    assert reserved_letters(MOUNTED) == ["D", "E", "F"]
    assert reserved_letters("") == []


def test_registry_patch_forgets_letters():
    patch = registry_patch(SETUP, 2, forget_letters=["D", "E"]).removesuffix(NO_ENCRYPTION)
    assert patch.endswith('[\\MountedDevices]\n"\\\\DosDevices\\\\D:"=-\n"\\\\DosDevices\\\\E:"=-\n')
    for bad in (["C"], ["DE"], ["1"]):
        with pytest.raises(ValueError):
            registry_patch(SETUP, 2, forget_letters=bad)


def test_personalize_forgets_the_image_pcs_letters(windows, runner):
    _windows_on(windows)
    select = runner.responses[("hivexregedit", "--export")]
    merged = {}

    def export(cmd):
        return (True, MOUNTED, "") if cmd[-1] == "\\MountedDevices" else select

    def merge(cmd):
        merged["reg"] = Path(cmd[3]).read_text(encoding="utf-8")
        return True, "", ""
    runner.responses[("hivexregedit", "--export")] = export
    runner.responses[("hivexregedit", "--merge")] = merge
    assert windows.personalize(DEVICE, SETUP) is None
    assert merged["reg"] == registry_patch(SETUP, 2, forget_letters=["D", "E", "F"])


# ── Add hardware: the boot NIC in another revision or board ──────────

def _sz(text: str) -> str:
    raw = (text + "\0").encode("utf-16-le")
    return "hex(1):" + ",".join(f"{b:02x}" for b in raw)


REV15 = "VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_15"
REV16 = "VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16"
OTHER_BOARD = "VEN_10EC&DEV_8168&SUBSYS_86771043&REV_15"
INSTANCE = "01000000684CE00000"
ENUM = (
    "Windows Registry Editor Version 5.00\r\n\r\n"
    "[\\ControlSet002\\Enum\\PCI]\r\n\r\n"
    # a device the image saw without a driver: never a source
    "[\\ControlSet002\\Enum\\PCI\\VEN_10EC&DEV_8168&SUBSYS_00000000&REV_01]\r\n\r\n"
    "[\\ControlSet002\\Enum\\PCI\\VEN_10EC&DEV_8168&SUBSYS_00000000&REV_01\\3&1]\r\n"
    f'"HardwareID"={_multi_sz("PCI\\VEN_10EC&DEV_8168&SUBSYS_00000000&REV_01")}\r\n\r\n'
    # vip-05's NIC, bound to the NDIS driver
    f"[\\ControlSet002\\Enum\\PCI\\{REV15}]\r\n\r\n"
    f"[\\ControlSet002\\Enum\\PCI\\{REV15}\\{INSTANCE}]\r\n"
    f'"HardwareID"={_multi_sz("PCI\\" + REV15, "PCI\\VEN_10EC&DEV_8168&SUBSYS_E0001458")}\r\n'
    f'"CompatibleIDs"={_multi_sz("PCI\\VEN_10EC&DEV_8168&REV_15", "PCI\\VEN_10EC&DEV_8168")}\r\n'
    '"Service"="rt640x64"\r\n'
    f'"Driver"={_sz("{4d36e972-e325-11ce-bfc1-08002be10318}\\0001")}\r\n\r\n'
    f"[\\ControlSet002\\Enum\\PCI\\{REV15}\\{INSTANCE}\\Device Parameters]\r\n"
    '"InterruptManagement"=dword:00000001\r\n\r\n'
    "[\\ControlSet002\\Enum\\PCI\\VEN_8086&DEV_A3A3&SUBSYS_50011458&REV_00]\r\n\r\n"
    "[\\ControlSet002\\Enum\\PCI\\VEN_8086&DEV_A3A3&SUBSYS_50011458&REV_00\\3&2]\r\n"
    '"Service"="pci"\r\n'
)


ASUS = "6&95d5d6a&0&0000020A"     # a bus-location instance ID: a board without the card's serial number


def test_nic_known_needs_the_card_installed_under_its_instance():
    assert nic_known(ENUM, REV15)                                   # installed (instance not reported yet)
    assert nic_known(ENUM, REV15, INSTANCE)                         # exactly this one
    assert not nic_known(ENUM, REV15, ASUS)                         # the same chip on another board
    assert not nic_known(ENUM, REV16)                               # another revision: Add new hardware
    assert not nic_known(ENUM, "VEN_10EC&DEV_8168&SUBSYS_00000000&REV_01")   # seen, never installed
    for bad in (("VEN_10EC&DEV_8168", None), (REV16, "bad instance\\x")):
        with pytest.raises(ValueError):
            nic_known(ENUM, *bad)


def test_nic_support_reads_the_image_read_only(windows, runner):
    _windows_on(windows, "hardware-check")
    select = runner.responses[("hivexregedit", "--export")]
    runner.responses[("hivexregedit", "--export")] = lambda cmd: (
        (True, ENUM, "") if cmd[-1].endswith("\\Enum\\PCI") else select)
    intel = "VEN_8086&DEV_15B8&SUBSYS_86721043&REV_00"
    cards = [(REV15, None), (REV15, INSTANCE), (REV15, ASUS), (REV16, None), (intel, None)]
    assert windows.nic_support(DEVICE, cards) == {
        (REV15, None): "ready", (REV15, INSTANCE): "ready",
        (REV15, ASUS): "missing", (REV16, None): "missing", (intel, None): "missing"}
    mounts = [c for c in runner.calls if c[0] == "ntfs-3g"]
    assert mounts and all(c[2] == "ro" for c in mounts)
    assert not any(c[:2] == ["hivexregedit", "--merge"] for c in runner.calls)
    assert windows.nic_support(DEVICE, [("not an id", None)]).startswith("Invalid network card")


def test_personalize_never_writes_a_network_card(windows, runner):
    _windows_on(windows, "pc-05")
    merged = {}

    def merge(cmd):
        merged["reg"] = Path(cmd[3]).read_text(encoding="utf-8")
        return True, "", ""
    runner.responses[("hivexregedit", "--merge")] = merge
    setup = OsSetup("pc-05", {})
    assert windows.personalize(DEVICE, setup) is None
    assert merged["reg"] == registry_patch(setup, 2)
    assert "\\Enum\\PCI" not in merged["reg"]


# ── Prepared once per OS version: agent, Fast Startup, DHCP, letters ─

def _agent(tmp_path, data: bytes = b"MZ agent 0.1.13") -> "AgentInstall":
    import hashlib

    from app.windows_setup import AgentInstall

    exe = tmp_path / "pkg" / "ggnet-agent.exe"
    exe.parent.mkdir(exist_ok=True)
    exe.write_bytes(data)
    return AgentInstall(exe=exe, sha256=hashlib.sha256(data).hexdigest(), server_url="http://192.168.0.40:8088/")


def _prep(tmp_path, **kw):
    from app.windows_setup import ImagePrep

    return ImagePrep(agent=_agent(tmp_path, **kw))


def _merged(runner) -> dict:
    merged = {}

    def merge(cmd):
        key = "reg" if Path(cmd[2]).name.upper() == "SYSTEM" else "software"
        merged[key] = Path(cmd[3]).read_text(encoding="utf-8")
        return True, "", ""
    runner.responses[("hivexregedit", "--merge")] = merge
    return merged


def _image(windows) -> Path:
    """An image's Windows as prepare_image mounts it."""
    root = _windows_on(windows, "prepare-win11").parents[3]
    (root / "Windows" / "System32" / "config" / "SOFTWARE").write_bytes(b"regf")
    return root


def test_image_patch_registers_the_agent_service():
    from app.windows_setup import agent_service_patch, image_patch

    patch = image_patch(2, agent_service=True)
    assert "[\\ControlSet002\\Services\\ggnet-agent]" in patch
    assert '"Start"=dword:00000002' in patch and '"ObjectName"="LocalSystem"' in patch
    image_path = '"C:\\Program Files\\ggnet-agent\\ggnet-agent.exe"\0'.encode("utf-16-le")
    assert '"ImagePath"=hex(2):' + ",".join(f"{b:02x}" for b in image_path) in patch
    # sc failure: restart after 5 s, 5 s and 30 s, reset after a day
    assert '"FailureActions"=hex:80,51,01,00,' in patch and ",30,75,00,00" in patch
    assert "[\\ControlSet002\\Control\\Session Manager\\Power]" in patch
    assert '"HiberbootEnabled"=dword:00000000' in patch
    assert agent_service_patch(1)[1] == "[\\ControlSet001\\Services\\ggnet-agent]"
    assert "ggnet-agent" not in image_patch(2)
    # The PC's own patch never carries image-wide things.
    assert "ggnet-agent" not in registry_patch(SETUP, 2)


def test_prepare_image_puts_the_agent_in_and_cleans_up(windows, runner, tmp_path):
    import json as _json

    root = _image(windows)
    program_files = root / "program files"            # any case, as on NTFS
    program_files.mkdir()
    select = runner.responses[("hivexregedit", "--export")]

    def export(cmd):
        if cmd[-1].endswith("\\Interfaces"):
            return True, INTERFACES, ""
        return (True, MOUNTED, "") if cmd[-1] == "\\MountedDevices" else select
    runner.responses[("hivexregedit", "--export")] = export
    merged = _merged(runner)
    seen = {}

    def snapshot(cmd):
        exe = program_files / "ggnet-agent" / "ggnet-agent.exe"
        seen["exe"] = exe.read_bytes() if exe.exists() else None
        settings = exe.with_name("appsettings.json")
        seen["settings"] = _json.loads(settings.read_text()) if settings.exists() else None
        return True, "", ""
    runner.responses[("umount",)] = snapshot

    assert windows.prepare_image(DEVICE, _prep(tmp_path), label="win11") is None
    assert seen["exe"] == b"MZ agent 0.1.13"
    assert seen["settings"]["Agent"]["ServerUrl"] == "http://192.168.0.40:8088"
    assert seen["settings"]["Agent"]["DriveLetter"] == "D"
    from app.windows_setup import image_patch
    assert merged["reg"] == image_patch(2, [FIXED], ["D", "E", "F"], agent_service=True, tuning=True)
    assert "ComputerName" not in merged["reg"]            # no PC's identity in the image


def test_the_same_agent_is_not_copied_again_and_own_settings_stay(windows, runner, tmp_path):
    folder = _image(windows) / "Program Files" / "ggnet-agent"
    folder.mkdir(parents=True)
    exe = folder / "ggnet-agent.exe"
    exe.write_bytes(b"MZ agent 0.1.13")
    (folder / "appsettings.json").write_text('{"Agent": {"ServerUrl": "http://10.0.0.1:8088", "HideLocalDisks": false}}')
    stamp = exe.stat().st_mtime_ns
    _merged(runner)
    seen = {}
    runner.responses[("umount",)] = lambda cmd: (seen.update(
        mtime=exe.stat().st_mtime_ns, settings=(folder / "appsettings.json").read_text()), (True, "", ""))[1]
    assert windows.prepare_image(DEVICE, _prep(tmp_path), label="win11") is None
    assert seen["mtime"] == stamp                                 # not written again
    assert "10.0.0.1" in seen["settings"]                         # the image's own settings stay


def test_a_new_agent_replaces_the_old_one(windows, runner, tmp_path):
    folder = _image(windows) / "Program Files" / "ggnet-agent"
    folder.mkdir(parents=True)
    (folder / "ggnet-agent.exe").write_bytes(b"MZ agent 0.1.11 (older and longer)")
    _merged(runner)
    seen = {}
    runner.responses[("umount",)] = lambda cmd: (seen.update(
        exe=(folder / "ggnet-agent.exe").read_bytes(), left=sorted(p.name for p in folder.iterdir())), (True, "", ""))[1]
    assert windows.prepare_image(DEVICE, _prep(tmp_path), label="win11") is None
    assert seen["exe"] == b"MZ agent 0.1.13"
    assert seen["left"] == ["appsettings.json", "ggnet-agent.exe"]   # no .new left behind


def test_an_image_without_program_files_gets_no_agent_service(windows, runner, tmp_path):
    _image(windows)
    merged = _merged(runner)
    assert windows.prepare_image(DEVICE, _prep(tmp_path), label="win11") == "no Program Files folder in the image"
    assert "ggnet-agent" not in merged["reg"]
    assert "HiberbootEnabled" in merged["reg"]                    # the tuning still goes in


def test_a_pcs_writeback_gets_only_its_identity(windows, runner):
    _windows_on(windows)
    merged = _merged(runner)
    assert windows.personalize(DEVICE, SETUP) is None
    assert "ggnet-agent" not in merged["reg"] and "HiberbootEnabled" not in merged["reg"]


# ── Diskless tuning (prepare_image) ──────────────────────────────────

NET_CLASS_EXPORT = (
    "Windows Registry Editor Version 5.00\r\n\r\n"
    "[\\ControlSet002\\Control\\Class\\{4d36e972-e325-11ce-bfc1-08002be10318}]\r\n"
    '"Class"="Net"\r\n\r\n'
    "[\\ControlSet002\\Control\\Class\\{4d36e972-e325-11ce-bfc1-08002be10318}\\0001]\r\n"
    '"DriverDesc"="Realtek PCIe GbE Family Controller"\r\n'
    '"*EEE"="1"\r\n"EnableGreenEthernet"="1"\r\n"*WakeOnMagicPacket"="1"\r\n\r\n'
    "[\\ControlSet002\\Control\\Class\\{4d36e972-e325-11ce-bfc1-08002be10318}\\0001\\Ndi]\r\n"
    '"*EEE"="1"\r\n\r\n'
    "[\\ControlSet002\\Control\\Class\\{4d36e972-e325-11ce-bfc1-08002be10318}\\0002]\r\n"
    '"DriverDesc"="WAN Miniport (IP)"\r\n'
)


def test_tuning_keeps_the_pc_awake_and_writing_little():
    from app.windows_setup import BALANCED, HIGH_PERFORMANCE, image_patch, tuning_patch

    patch = image_patch(2, tuning=True)
    schemes = "[\\ControlSet002\\Control\\Power\\User\\PowerSchemes"
    assert f'{schemes}]\n"ActivePowerScheme"="{HIGH_PERFORMANCE}"' in patch
    for scheme in (HIGH_PERFORMANCE, BALANCED):
        # the display never turns off; PCIe link power management off
        assert (f"{schemes}\\{scheme}\\7516b95f-f776-4464-8c53-06167f40cc99\\3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e]\n"
                '"ACSettingIndex"=dword:00000000\n"DCSettingIndex"=dword:00000000') in patch
        assert f"{schemes}\\{scheme}\\501a4d13-42af-4429-9fd1-a8218c268e20\\ee12f906-d277-404b-b6da-e5fa1a576df5]" in patch
    assert '"HibernateEnabled"=dword:00000000' in patch and '"HiberbootEnabled"=dword:00000000' in patch
    page_files = "c:\\pagefile.sys 4096 4096\0\0".encode("utf-16-le")
    assert '"PagingFiles"=hex(7):' + ",".join(f"{b:02x}" for b in page_files) in patch
    assert '"ClearPageFileAtShutdown"=dword:00000000' in patch
    assert '[\\ControlSet002\\Control\\CrashControl]\n"CrashDumpEnabled"=dword:00000000' in patch
    assert ('[\\ControlSet002\\Services\\iScsiPrt\\Parameters]\n"WaitForNetworkAtBoot"=dword:00000000\n'
            '"DelayForNetworkAtBoot"=dword:00000001') in patch
    assert '"ArpRetryCount"=dword:00000000\n"DisableDhcpMediaSense"=dword:00000001' in patch
    assert "ServiceGroupOrder" not in patch                # its order was not given
    assert '[\\ControlSet002\\Services\\WSearch]\n"Start"=dword:00000004' in patch
    assert '[\\ControlSet002\\Services\\SysMain]\n"Start"=dword:00000004' in patch
    assert tuning_patch(1)[1] == "[\\ControlSet001\\Control\\Power\\User\\PowerSchemes]"
    # hivexregedit creates one level at a time: every key's parent is named before it.
    keys = [line[1:-1] for line in patch.splitlines() if line.startswith("[\\ControlSet002\\Control\\Power\\User")]
    for key in keys[1:]:
        assert key.rsplit("\\", 1)[0] in keys[: keys.index(key)], key


def test_tuning_turns_off_the_network_cards_power_saving_but_not_wake_on_lan():
    from app.windows_setup import image_patch

    patch = image_patch(2, tuning=True, net_class_export=NET_CLASS_EXPORT)
    key = "[\\ControlSet002\\Control\\Class\\{4d36e972-e325-11ce-bfc1-08002be10318}"
    assert f'{key}\\0001]\n"*EEE"="0"\n"EnableGreenEthernet"="0"\n' in patch
    assert "WakeOnMagicPacket" not in patch
    assert f"{key}\\0002]" not in patch                  # nothing to turn off there
    assert "\\Ndi]" not in patch                         # the driver's own definitions stay


def test_software_patch_stops_automatic_updates_and_maintenance():
    from app.windows_setup import software_patch

    patch = software_patch()
    assert patch.startswith("Windows Registry Editor Version 5.00\n")
    assert '[\\Policies\\Microsoft\\Windows\\WindowsUpdate\\AU]\n"NoAutoUpdate"=dword:00000001' in patch
    assert '"ExcludeWUDriversInQualityUpdate"=dword:00000001' in patch
    assert '[\\Policies\\Microsoft\\WindowsStore]\n"AutoDownload"=dword:00000002' in patch
    assert '"MaintenanceDisabled"=dword:00000001' in patch


def test_prepare_image_tunes_both_hives_and_drops_the_hibernation_file(windows, runner, tmp_path):
    from app.windows_setup import image_patch, software_patch

    root = _image(windows)
    (root / "program files").mkdir()
    (root / "HIBERFIL.SYS").write_bytes(b"hibr")
    select = runner.responses[("hivexregedit", "--export")]
    runner.responses[("hivexregedit", "--export")] = lambda cmd: (
        (True, NET_CLASS_EXPORT, "") if cmd[-1].endswith("bfc1-08002be10318}") else select)
    merged = _merged(runner)
    assert windows.prepare_image(DEVICE, _prep(tmp_path), label="win11") is None
    assert merged["reg"] == image_patch(2, agent_service=True, tuning=True, net_class_export=NET_CLASS_EXPORT)
    assert merged["software"] == software_patch()
    assert not (root / "HIBERFIL.SYS").exists()
    config = root / "Windows" / "System32" / "config"
    merges = [c[2] for c in runner.calls if c[:2] == ["hivexregedit", "--merge"]]
    assert merges == [str(config / "SYSTEM"), str(config / "SOFTWARE")]


def test_prepare_image_without_tuning_leaves_windows_as_it_is(windows, runner, tmp_path):
    from app.windows_setup import ImagePrep

    root = _image(windows)
    (root / "Program Files").mkdir()
    (root / "hiberfil.sys").write_bytes(b"hibr")
    merged = _merged(runner)
    prep = ImagePrep(agent=_agent(tmp_path), tuning=False)
    assert windows.prepare_image(DEVICE, prep, label="win11") is None
    assert "PowerSchemes" not in merged["reg"] and "software" not in merged
    assert (root / "hiberfil.sys").exists()


def test_defender_off_lines_and_policy():
    from app.windows_setup import defender_off_lines, defender_policy_patch

    reg = defender_off_lines(2, ["WinDefend", "WdFilter"])
    assert "[\\ControlSet002\\Services\\WinDefend]\n\"Start\"=dword:00000004" in reg
    assert "[\\ControlSet002\\Services\\WdFilter]" in reg and "WdBoot" not in reg
    with pytest.raises(ValueError):
        defender_off_lines(2, ["Tcpip"])
    policy = defender_policy_patch()
    assert '"DisableRealtimeMonitoring"=dword:00000001' in policy
    assert '"DisableNotifications"=dword:00000001' in policy
    # Each policy key's parent comes first: hivexregedit creates one level at a time.
    assert policy.index("[\\Policies\\Microsoft]") < policy.index("[\\Policies\\Microsoft\\Windows Defender]")


def test_prepare_image_turns_defender_off_only_when_asked(windows, runner, tmp_path):
    from app.windows_setup import ImagePrep, defender_off_lines, defender_policy_patch

    root = _image(windows)
    (root / "Program Files").mkdir()
    select = runner.responses[("hivexregedit", "--export")]
    present = {"WinDefend", "WdBoot", "WdFilter"}             # no MDCoreSvc, Sense ... in this image

    def export(cmd):
        name = cmd[-1].rsplit("\\", 1)[-1]
        if "\\Services\\" in cmd[-1] and name in ("WinDefend", "WdNisSvc", "WdNisDrv", "WdFilter",
                                                       "WdBoot", "Sense", "MDCoreSvc"):
            return (True, "x", "") if name in present else (False, "", "not found")
        return select
    runner.responses[("hivexregedit", "--export")] = export
    merges = []

    def merge(cmd):
        merges.append((Path(cmd[2]).name, Path(cmd[3]).read_text(encoding="utf-8")))
        return True, "", ""
    runner.responses[("hivexregedit", "--merge")] = merge

    assert windows.prepare_image(DEVICE, ImagePrep(agent=_agent(tmp_path), defender_off=True), label="win11") is None
    assert ("SYSTEM", defender_off_lines(2, ["WinDefend", "WdFilter", "WdBoot"])) in merges
    assert ("SOFTWARE", defender_policy_patch()) in merges

    merges.clear()
    assert windows.prepare_image(DEVICE, _prep(tmp_path), label="win11") is None
    assert not any("Windows Defender" in text or "WinDefend" in text for _, text in merges)


def test_an_image_without_a_software_hive_is_reported(windows, runner, tmp_path):
    root = _windows_on(windows, "prepare-win11").parents[3]
    (root / "Program Files").mkdir()
    merged = _merged(runner)
    assert windows.prepare_image(DEVICE, _prep(tmp_path), label="win11") == "no SOFTWARE hive in the image"
    assert "PowerSchemes" in merged["reg"]               # the SYSTEM part is in



def _group_order_export(groups: list[str]) -> str:
    data = ("\0".join(groups) + "\0\0").encode("utf-16-le")
    return ("Windows Registry Editor Version 5.00\r\n\r\n[\\ControlSet002\\Control\\ServiceGroupOrder]\r\n"
            '"List"=hex(7):' + ",".join(f"{b:02x}" for b in data) + "\r\n")


WINDOWS_ORDER = ["System Reserved", "EMS", "WdfLoadGroup", "Boot Bus Extender", "System Bus Extender",
                 "SCSI miniport", "Port", "Primary Disk", "Filter", "Boot File System", "Base",
                 "Video", "NDIS Wrapper", "COM Infrastructure", "PlugPlay", "PNP_TDI", "NDIS", "TDI"]


def _order_in(patch: str) -> list[str]:
    raw = patch.split('[\\ControlSet002\\Control\\ServiceGroupOrder]\n"List"=hex(7):')[1].split("\n")[0]
    return [g for g in bytes(int(x, 16) for x in raw.split(",")).decode("utf-16-le").split("\0") if g]


def test_the_iscsi_initiator_loads_after_the_network_stack():
    from app.windows_setup import ISCSI_GROUP, image_patch

    patch = image_patch(2, tuning=True, group_order_export=_group_order_export(WINDOWS_ORDER))
    assert _order_in(patch) == [
        "System Reserved", "EMS", "WdfLoadGroup", "Boot Bus Extender", "System Bus Extender",
        "NDIS Wrapper", "NDIS", "Base", "PNP_TDI", ISCSI_GROUP,
        "SCSI miniport", "Port", "Primary Disk", "Filter", "Boot File System",
        "Video", "COM Infrastructure", "PlugPlay", "TDI"]
    assert f'[\\ControlSet002\\Services\\iScsiPrt]\n"Group"="{ISCSI_GROUP}"' in patch
    # Prepared twice: the same order, the group once.
    again = image_patch(2, tuning=True, group_order_export=_group_order_export(_order_in(patch)))
    assert _order_in(again) == _order_in(patch)


def test_an_unknown_group_order_is_left_alone():
    from app.windows_setup import image_patch

    no_tcpip = [g for g in WINDOWS_ORDER if g != "PNP_TDI"]
    for export in ("", "garbage", _group_order_export(no_tcpip)):
        patch = image_patch(2, tuning=True, group_order_export=export)
        assert "ServiceGroupOrder" not in patch and "iScsiPrt]\n" not in patch



def test_a_spare_is_set_up_under_its_own_label(windows, runner):
    _windows_on(windows, "vip-05-spare")
    merged = _merged(runner)
    assert windows.personalize(DEVICE, SETUP, label="vip-05-spare") is None
    mount = str(windows.run_dir / "vip-05-spare")
    assert ["ntfs-3g", "-o", "rw,noatime", "/dev/zd16p3", mount] in runner.calls
    assert any(c[:2] == ["hivexregedit", "--merge"] and c[3].endswith("vip-05-spare.reg") for c in runner.calls)
    assert '"ComputerName"="VIP-05"' in merged["reg"]


# ── Other diskless systems ────────────────────────────────────────────

def _msz(*items: str) -> str:
    data = ("\0".join(items) + "\0\0").encode("utf-16-le")
    return "hex(7):" + ",".join(f"{b:02x}" for b in data)


def _sz(text: str) -> str:
    return "hex(1):" + ",".join(f"{b:02x}" for b in (text + "\0").encode("utf-16-le"))


CLASS_EXPORT = "\r\n".join([
    "Windows Registry Editor Version 5.00", "",
    "[\\ControlSet001\\Control\\Class\\{71a27cdd-812a-11d0-bec7-08002be2092f}]",
    f'"UpperFilters"={_msz("CCacheX", "volsnap")}',
    f'"LowerFilters"={_msz("fvevol", "iorate", "rdyboost")}', "",
    "[\\ControlSet001\\Control\\Class\\{4d36e972-e325-11ce-bfc1-08002be10318}]",
    f'"UpperFilters"={_msz("CCBootPNPX")}', "",
    "[\\ControlSet001\\Control\\Class\\{4d36e972-e325-11ce-bfc1-08002be10318}\\0001]",
    f'"UpperFilters"={_msz("CCBootPNPX")}',            # a device's own key: not a class filter list
    "",
])

ENUM_EXPORT = "\r\n".join([
    "Windows Registry Editor Version 5.00", "",
    f"[\\ControlSet001\\Enum\\PCI\\{REV15}\\01000000684CE00000]",
    f'"ClassGUID"={_sz("{4d36e972-e325-11ce-bfc1-08002be10318}")}',
    f'"Service"={_sz("rt640x64")}', "",
    "[\\ControlSet001\\Enum\\PCI\\VEN_8086&DEV_A394\\3&11583659&0&E0]",
    f'"ClassGUID"={_sz("{4d36e97d-e325-11ce-bfc1-08002be10318}")}',
    f'"Service"={_sz("pci")}', "",
])


def test_foreign_drivers_leave_the_filter_lists_and_cards_are_marked_for_a_network_boot():
    from app.windows_setup import diskless_boot_lines, nic_services

    assert nic_services(ENUM_EXPORT) == ["rt640x64"]
    reg = "\n".join(diskless_boot_lines(1, CLASS_EXPORT, ENUM_EXPORT, ["CCacheX", "CCBootPNPX"]))
    volume = "[\\ControlSet001\\Control\\Class\\{71a27cdd-812a-11d0-bec7-08002be2092f}]"
    net = "[\\ControlSet001\\Control\\Class\\{4d36e972-e325-11ce-bfc1-08002be10318}]"
    assert f'{volume}\n"UpperFilters"={_msz("volsnap")}' in reg            # CCacheX gone, volsnap kept
    assert "LowerFilters" not in reg                                         # untouched list not rewritten
    assert f'{net}\n"UpperFilters"=-' in reg                                  # emptied list deleted
    assert "\\0001]" not in reg
    assert '[\\ControlSet001\\Services\\CCacheX]\n"Start"=dword:00000004' in reg
    assert '[\\ControlSet001\\Services\\rt640x64]\n"BootFlags"=dword:00000001' in reg


def test_a_clean_image_gets_only_the_network_boot_flags():
    from app.windows_setup import diskless_boot_lines

    reg = "\n".join(diskless_boot_lines(1, CLASS_EXPORT.replace("CCacheX", "Other"), ENUM_EXPORT, []))
    assert "Class" not in reg and "Start" not in reg and "BootFlags" in reg


def test_prepare_image_turns_other_diskless_drivers_off(windows, runner, tmp_path):
    root = _image(windows)
    (root / "Program Files").mkdir()
    select = runner.responses[("hivexregedit", "--export")]

    def export(cmd):
        key = cmd[-1]
        if key.endswith("\\Control\\Class"):
            return True, CLASS_EXPORT.replace("ControlSet001", "ControlSet002"), ""
        if key.endswith("\\Enum\\PCI"):
            return True, ENUM_EXPORT.replace("ControlSet001", "ControlSet002"), ""
        if key.endswith(("\\Services\\CCacheX", "\\Services\\CCBootPNPX", "\\Services\\CCBootClient")):
            return True, f"Windows Registry Editor Version 5.00\r\n\r\n[{key}]\r\n", ""
        if "\\Services\\CC" in key:
            return False, "", "not found"
        return select
    runner.responses[("hivexregedit", "--export")] = export
    merges = []

    def merge(cmd):
        merges.append(Path(cmd[3]).read_text(encoding="utf-8"))
        return True, "", ""
    runner.responses[("hivexregedit", "--merge")] = merge
    assert windows.prepare_image(DEVICE, _prep(tmp_path), label="win11") is None
    diskless = next(m for m in merges if "CCacheX" in m)
    assert '[\\ControlSet002\\Services\\CCBootClient]\n"Start"=dword:00000004' in diskless
    assert "CCopyFile" not in diskless                                       # not in this image
    assert '[\\ControlSet002\\Services\\rt640x64]\n"BootFlags"=dword:00000001' in diskless


# ── Add new hardware: the toolchain job ──────────────────────────────

I219 = "VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10"


def test_pci_ids_as_windows_reads_them_from_a_network_card():
    from app.windows_setup import pci_ids

    hardware, compatible = pci_ids(I219)
    assert hardware[0] == "PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10"
    assert "PCI\\VEN_8086&DEV_15BC&CC_020000" in hardware
    assert compatible[:2] == ["PCI\\VEN_8086&DEV_15BC&REV_10", "PCI\\VEN_8086&DEV_15BC"]
    with pytest.raises(ValueError):
        pci_ids("VEN_8086&DEV_15BC")


def test_toolchain_job_names_the_card_under_its_instance():
    from app.windows_setup import NET_CLASS, toolchain_job

    job = toolchain_job([(I219, "3&11583659&0&FE")])
    device = job["devices"][0]
    assert device["instance_id"] == f"PCI\\{I219}\\3&11583659&0&FE"
    assert device["class_guid"] == NET_CLASS
    for bad in ([(I219, "")], [(I219, "a\\b")], []):
        with pytest.raises(ValueError):
            toolchain_job(bad)


def _toolchain_image(windows) -> Path:
    root = _windows_on(windows, "toolchain-win11").parents[3]
    (root / "Program Files").mkdir()
    (root / "ProgramData").mkdir()
    return root


def test_start_toolchain_puts_the_agent_and_the_job_in(windows, runner, tmp_path):
    root = _toolchain_image(windows)
    folder = root / "ProgramData" / "ggNet" / "toolchain"
    folder.mkdir(parents=True)
    (folder / "result.json").write_text("{}")             # from an earlier run
    merged = _merged(runner)

    assert windows.start_toolchain(DEVICE, [(I219, "3&11583659&0&FE")], _prep(tmp_path), label="win11") is None

    assert (root / "Program Files" / "ggnet-agent" / "ggnet-agent.exe").read_bytes() == b"MZ agent 0.1.13"
    job = json.loads((folder / "job.json").read_text())
    assert job["devices"][0]["instance_id"] == f"PCI\\{I219}\\3&11583659&0&FE"
    assert not (folder / "result.json").exists()
    assert "[\\ControlSet002\\Services\\ggnet-agent]" in merged["reg"]
    assert "[\\ControlSet002\\Services\\storahci]" in merged["reg"]
    assert "[-\\ControlSet002\\Services\\storahci\\StartOverride]" in merged["reg"]
    assert ["ntfs-3g", "-o", "rw,noatime", "/dev/zd16p3", str(windows.run_dir / "toolchain-win11")] in runner.calls


def test_start_toolchain_needs_the_agent_and_a_valid_card(windows, runner, tmp_path):
    from app.windows_setup import ImagePrep

    assert "agent" in windows.start_toolchain(DEVICE, [(I219, "3&1&0&FE")], ImagePrep(agent=None))
    assert "instance" in windows.start_toolchain(DEVICE, [(I219, "")], _prep(tmp_path))
    assert not any(c[0] == "ntfs-3g" for c in runner.calls)


def test_toolchain_result_is_read_and_the_job_removed(windows, runner):
    folder = _toolchain_image(windows) / "ProgramData" / "ggNet" / "toolchain"
    folder.mkdir(parents=True)
    (folder / "job.done").write_text("{}")
    result = {"ok": True, "devices": [{"instance_id": f"PCI\\{I219}\\3&1&0&FE", "ok": True, "driver": "Intel I219-V"}]}
    (folder / "result.json").write_text("﻿" + json.dumps(result), encoding="utf-8")

    assert windows.toolchain_result(DEVICE, label="win11") == result
    assert not any(folder.iterdir())


def test_toolchain_result_without_a_result(windows, runner):
    _toolchain_image(windows)
    assert "no result" in windows.toolchain_result(DEVICE, label="win11")


def test_a_card_whose_install_failed_is_not_ready():
    # Windows left the entry without a driver (ConfigFlags 0x20, reinstall): it cannot start the card.
    failed = ENUM + (
        "\n[\\ControlSet001\\Enum\\PCI\\VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16]\n"
        "\n[\\ControlSet001\\Enum\\PCI\\VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16\\01000000684CE00000]\n"
        '"ConfigFlags"=dword:00000020\n')
    assert not nic_known(failed, REV16, INSTANCE)
    assert not nic_known(failed, REV16)


# ── Background services off (faster startup) ─────────────────────────

def test_background_services_finds_the_listed_services_and_browser_updaters():
    from app.windows_setup import background_services

    export = "\n".join(f"[\\ControlSet002\\Services\\{n}]\n\"Start\"=dword:00000002\n" for n in (
        "DoSvc", "WaaSMedicSvc", "DiagTrack", "TokenBroker", "SENS", "XblAuthManager", "wuauserv",
        "GoogleUpdaterService149.0.7814.0", "edgeupdatem", "Tcpip"))
    export += "[\\ControlSet002\\Services\\DoSvc\\Parameters]\n"
    found = background_services(export, 2)
    assert found == ["DoSvc", "WaaSMedicSvc", "DiagTrack", "GoogleUpdaterService149.0.7814.0", "edgeupdatem"]
    # What games, Store and Microsoft/Xbox sign-in need, and Windows Update itself, stay.
    assert not {"TokenBroker", "SENS", "XblAuthManager", "wuauserv"} & set(found)


def test_prepare_image_turns_background_services_off_only_when_asked(windows, runner, tmp_path):
    from app.windows_setup import ImagePrep, services_off_lines, services_policy_patch

    (_image(windows) / "Program Files").mkdir()
    select = runner.responses[("hivexregedit", "--export")]
    services = "[\\ControlSet002\\Services\\DoSvc]\n\"Start\"=dword:00000003\n"
    runner.responses[("hivexregedit", "--export")] = lambda cmd: (
        (True, services, "") if cmd[-1] == "\\ControlSet002\\Services" else select)
    merges = []

    def merge(cmd):
        merges.append((Path(cmd[2]).name.upper(), Path(cmd[3]).read_text(encoding="utf-8")))
        return True, "", ""
    runner.responses[("hivexregedit", "--merge")] = merge
    assert windows.prepare_image(DEVICE, ImagePrep(agent=_agent(tmp_path), services_off=True), label="win11") is None
    assert ("SYSTEM", services_off_lines(2, ["DoSvc"])) in merges
    assert ("SOFTWARE", services_policy_patch()) in merges
    assert '"NtfsDisableLastAccessUpdate"=dword:80000001' in services_off_lines(2, [])
    merges.clear()
    assert windows.prepare_image(DEVICE, _prep(tmp_path), label="win11") is None
    assert not any("DoSvc" in text for _, text in merges)


def test_installed_nics_lists_the_net_cards_with_a_driver_and_their_model():
    from app.windows_setup import installed_nics

    desc = "@net1ic64.inf,%e15bcnc.devicedesc%;Intel(R) Ethernet Connection (7) I219-V"
    utf16 = ",".join(f"{b:02x}" for b in (desc + "\0").encode("utf-16-le"))
    export = ENUM + (
        "\r\n[\\ControlSet002\\Enum\\PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10]\r\n\r\n"
        "[\\ControlSet002\\Enum\\PCI\\VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10\\3&11583659&0&FE]\r\n"
        f'"ClassGUID"="{{4d36e972-e325-11ce-bfc1-08002be10318}}"\r\n"Service"="e1i68x64"\r\n'
        f'"DeviceDesc"=hex(1):{utf16}\r\n'
        "\r\n[\\ControlSet002\\Enum\\PCI\\VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16]\r\n\r\n"
        "[\\ControlSet002\\Enum\\PCI\\VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16\\01000000684CE00000]\r\n"
        '"ClassGUID"="{4d36e972-e325-11ce-bfc1-08002be10318}"\r\n"Service"="rt640x64"\r\n'
        '"FriendlyName"="Realtek Gaming GbE Family Controller #2"\r\n')
    assert installed_nics(export) == [
        ("VEN_8086&DEV_15BC&SUBSYS_86721043&REV_10", "3&11583659&0&FE", "Intel(R) Ethernet Connection (7) I219-V"),
        ("VEN_10EC&DEV_8168&SUBSYS_E0001458&REV_16", "01000000684CE00000", "Realtek Gaming GbE Family Controller"),
    ]   # the REV_15 card in ENUM has no ClassGUID: not a Net class device here; the chipset device is not a card


def test_bitlocker_windows_is_named_not_mounted(windows, runner):
    """Windows encrypted itself (Device encryption): say so instead of "no Windows"."""
    runner.responses[("lsblk",)] = (True, _lsblk(("/dev/zd16p3", "BitLocker", 90 << 30),
                                               ("/dev/zd16p4", "ntfs", 900 << 20)), "")
    problem = windows.personalize(DEVICE, SETUP)
    assert "encrypted with BitLocker (/dev/zd16p3)" in problem
    assert not any(c[0] == "ntfs-3g" for c in runner.calls)


def test_encryption_since_the_disk_appeared_is_seen(windows, runner):
    """udev still says ntfs; the disk itself (blkid -p) says BitLocker."""
    runner.responses[("blkid", "-p", "-o", "value", "-s", "TYPE", "/dev/zd16p3")] = (True, "BitLocker\n", "")
    assert windows.bitlocker_partitions(DEVICE) == ["/dev/zd16p3"]
    assert "/dev/zd16p3" not in windows.ntfs_partitions(DEVICE)
