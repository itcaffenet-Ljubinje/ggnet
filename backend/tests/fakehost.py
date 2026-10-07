"""
FakeHost: an in-memory simulation of ZFS and LIO/targetcli for service and
API tests. It behaves like the real host in the cases confirmed by manual
tests on a Proxmox host:

  - destroying a zvol that a backstore holds → "dataset is busy"
  - destroying a snapshot with a hold or a dependent clone → error
  - cloning a snapshot that does not exist → error

It implements only the commands the managers actually send. An unknown
command raises AssertionError, so the test fails instead of passing silently.
"""

from __future__ import annotations

from typing import Optional


class FakeHost:
    def __init__(self) -> None:
        self.datasets: dict[str, dict] = {}         # name → {"origin": snap|None, "readonly": bool}
        self.snapshots: dict[str, set[str]] = {}    # "ds@snap" → holds
        self.backstores: dict[str, str] = {}        # name → /dev/zvol/...
        self.targets: set[str] = set()
        self.fail_on: dict[tuple[str, ...], str] = {}   # command prefix → stderr
        self.calls: list[list[str]] = []
        self.last_error = ""

    # ── Runner interface ──────────────────────────────────────────────

    def run(self, cmd: list[str], timeout: Optional[int] = None, quiet: bool = False):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        for prefix, err in self.fail_on.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return self._err(err)
        if cmd[0] == "zfs":
            return self._zfs(cmd[1:])
        if cmd[0] == "targetcli":
            return self._targetcli(cmd[1:])
        raise AssertionError(f"FakeHost does not know command: {cmd}")

    def _ok(self, out: str = ""):
        return True, out, ""

    def _err(self, err: str):
        self.last_error = err
        return False, "", err

    # ── Helpers ───────────────────────────────────────────────────────

    def add_dataset(self, name: str, origin: Optional[str] = None) -> None:
        self.datasets[name] = {"origin": origin, "readonly": False}

    def exists(self, name: str) -> bool:
        return name in self.datasets or name in self.snapshots

    def clones_of(self, snap: str) -> list[str]:
        return [d for d, p in self.datasets.items() if p["origin"] == snap]

    def _children(self, name: str) -> list[str]:
        return [d for d in self.datasets if d.startswith(name + "/")]

    def _busy(self, name: str) -> bool:
        return f"/dev/zvol/{name}" in self.backstores.values()

    # ── zfs ───────────────────────────────────────────────────────────

    def _zfs(self, a: list[str]):
        sub = a[0]
        if sub == "list":
            if "-t" in a and a[a.index("-t") + 1] == "snapshot":
                ds = a[-1]
                if ds not in self.datasets:
                    return self._err(f"cannot open '{ds}': dataset does not exist")
                lines = [f"{s}\t{','.join(self.clones_of(s)) or '-'}"
                         for s in self.snapshots if s.split("@")[0] == ds]
                return self._ok("\n".join(lines))
            name = a[-1]
            if not self.exists(name):
                return self._err(f"cannot open '{name}': dataset does not exist")
            return self._ok(name)

        if sub == "create":
            name = a[-1]
            if self.exists(name):
                return self._err(f"cannot create '{name}': dataset already exists")
            self.add_dataset(name)
            return self._ok()

        if sub == "snapshot":
            snap = a[-1]
            ds = snap.split("@")[0]
            if ds not in self.datasets:
                return self._err(f"cannot open '{ds}': dataset does not exist")
            if snap in self.snapshots:
                return self._err(f"cannot create snapshot '{snap}': dataset already exists")
            self.snapshots[snap] = set()
            return self._ok()

        if sub == "hold":
            tag, snap = a[1], a[2]
            if snap not in self.snapshots:
                return self._err(f"cannot hold snapshot '{snap}': dataset does not exist")
            if tag in self.snapshots[snap]:
                return self._err(f"cannot hold snapshot '{snap}': tag already exists")
            self.snapshots[snap].add(tag)
            return self._ok()

        if sub == "release":
            tag, snap = a[1], a[2]
            if tag not in self.snapshots.get(snap, set()):
                return self._err(f"cannot release hold from snapshot '{snap}': no such tag")
            self.snapshots[snap].discard(tag)
            return self._ok()

        if sub == "set":
            prop, name = a[1], a[2]
            if name not in self.datasets:
                return self._err(f"cannot open '{name}': dataset does not exist")
            key, _, value = prop.partition("=")
            if key == "readonly":
                self.datasets[name]["readonly"] = value == "on"
            return self._ok()

        if sub == "get":
            name = a[-1]
            if name not in self.datasets:
                return self._err(f"cannot open '{name}': dataset does not exist")
            return self._ok("-")

        if sub == "clone":
            src, dst = a[-2], a[-1]
            if src not in self.snapshots:
                return self._err(f"cannot open '{src}': dataset does not exist")
            if self.exists(dst):
                return self._err(f"cannot create '{dst}': dataset already exists")
            self.add_dataset(dst, origin=src)
            return self._ok()

        if sub == "destroy":
            name = a[-1]
            recursive = "-r" in a
            if not self.exists(name):
                return self._err(f"cannot open '{name}': dataset does not exist")
            victims = [name] + (self._children(name) if recursive else [])
            snaps = [s for s in self.snapshots
                     if any(s.split("@")[0] == v for v in victims)]
            if snaps and not recursive and name in self.datasets:
                return self._err(f"cannot destroy '{name}': filesystem has children")
            for v in victims:
                if self._busy(v):
                    return self._err(f"cannot destroy '{v}': dataset is busy")
            for s in snaps:
                if self.snapshots[s]:
                    return self._err(f"cannot destroy snapshot {s}: dataset is busy")
                if self.clones_of(s):
                    return self._err(f"cannot destroy '{name}': filesystem has dependent clones")
            for s in snaps:
                del self.snapshots[s]
            for v in victims:
                self.datasets.pop(v, None)
            return self._ok()

        raise AssertionError(f"FakeHost does not know zfs command: {a}")

    # ── targetcli ─────────────────────────────────────────────────────

    def _targetcli(self, a: list[str]):
        if a == ["saveconfig"]:
            return self._ok()
        path = a[0]

        if path == "/backstores/block" and a[1] == "create":
            name = a[2].split("=", 1)[1]
            dev = a[3].split("=", 1)[1]
            if name in self.backstores:
                return self._err(f"Storage object block/{name} exists")
            if dev.removeprefix("/dev/zvol/") not in self.datasets:
                return self._err(f"Device {dev} does not exist")
            self.backstores[name] = dev
            return self._ok()
        if path == "/backstores/block" and a[1] == "delete":
            if a[2] not in self.backstores:
                return self._err(f"No storage object named {a[2]}")
            del self.backstores[a[2]]
            return self._ok()

        if path == "/iscsi" and a[1] == "create":
            if a[2] in self.targets:
                return self._err(f"This Target already exists in configFS")
            self.targets.add(a[2])
            return self._ok()
        if path == "/iscsi" and a[1] == "delete":
            if a[2] not in self.targets:
                return self._err(f"No such Target in configfs")
            self.targets.discard(a[2])
            return self._ok()
        if path == "/iscsi" and a[1] == "ls":
            lines = [f"o- iscsi .... [Targets: {len(self.targets)}]"]
            lines += [f"  o- {t} .... [TPGs: 1]" for t in sorted(self.targets)]
            return self._ok("\n".join(lines))

        if path.startswith("/iscsi/"):
            iqn = path.split("/")[2]
            if iqn not in self.targets:
                return self._err(f"No such path {path}")
            return self._ok()

        raise AssertionError(f"FakeHost does not know targetcli command: {a}")
