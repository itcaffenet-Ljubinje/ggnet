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
        # iqn → {luns: {tpg lun: backstore}, acls: {initiator: {mapped: tpg lun}},
        #        portals: {"ip:port"}, attrs: {}}
        self.targets: dict[str, dict] = {}
        self.sessions: set[str] = set()             # initiators logged in (any target)
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
        if cmd[0] == "cat":
            return self._cat(cmd[1])
        raise AssertionError(f"FakeHost does not know command: {cmd}")

    def run_pipe(self, producer: list[str], consumer: list[str], timeout: Optional[int] = None):
        """Only `zfs send -i base clone@snap | zfs recv master` is simulated."""
        producer, consumer = [str(c) for c in producer], [str(c) for c in consumer]
        self.calls.append(producer + ["|"] + consumer)
        for prefix, err in self.fail_on.items():
            if tuple(producer[: len(prefix)]) == prefix or tuple(consumer[: len(prefix)]) == prefix:
                return self._err(err)
        assert producer[:3] == ["zfs", "send", "-i"] and consumer[:2] == ["zfs", "recv"], (producer, consumer)
        base, src, master = producer[3], producer[4], consumer[2]
        if base not in self.snapshots or src not in self.snapshots:
            return self._err("cannot send: snapshot does not exist")
        if self.datasets.get(src.split("@")[0], {}).get("origin") != base:
            return self._err("cannot send: incremental source is not an earlier snapshot or origin")
        newest = [s for s in self.snapshots if s.split("@")[0] == master][-1:]
        if newest != [base]:
            return self._err(f"cannot receive incremental stream: destination {master} has been modified")
        self.snapshots[f"{master}@{src.split('@')[1]}"] = set()
        return self._ok()

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
                mine = [s for s in self.snapshots if s.split("@")[0] == ds]   # creation order
                if a[a.index("-o") + 1] == "name":
                    return self._ok("\n".join(mine))
                lines = [f"{s}\t{','.join(self.clones_of(s)) or '-'}" for s in mine]
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
            self.datasets[name].setdefault("props", {})[key] = value
            return self._ok()

        if sub == "get":
            name = a[-1]
            if name not in self.datasets:
                return self._err(f"cannot open '{name}': dataset does not exist")
            if a[-2] == "readonly":
                return self._ok("on" if self.datasets[name]["readonly"] else "off")
            return self._ok("-")

        if sub == "clone":
            src, dst = a[-2], a[-1]
            if src not in self.snapshots:
                return self._err(f"cannot open '{src}': dataset does not exist")
            if self.exists(dst):
                return self._err(f"cannot create '{dst}': dataset already exists")
            self.add_dataset(dst, origin=src)
            return self._ok()

        if sub == "rename":
            src, dst = a[1], a[2]
            if src not in self.snapshots:
                return self._err(f"cannot open '{src}': dataset does not exist")
            if dst in self.snapshots:
                return self._err(f"cannot rename to '{dst}': dataset already exists")
            self.snapshots = {(dst if k == src else k): v for k, v in self.snapshots.items()}
            return self._ok()

        if sub == "destroy":
            name = a[-1]
            recursive = "-r" in a
            if not self.exists(name):
                return self._err(f"cannot open '{name}': dataset does not exist")
            if "@" in name:
                if self.snapshots[name]:
                    return self._err(f"cannot destroy snapshot {name}: dataset is busy")
                if self.clones_of(name):
                    return self._err(f"cannot destroy '{name}': snapshot has dependent clones")
                del self.snapshots[name]
                return self._ok()
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

    # ── configfs (cat) ────────────────────────────────────────────────

    def _cat(self, path: str):
        # .../iscsi/<target>/tpgt_1/acls/<initiator>/info
        parts = path.split("/")
        if len(parts) < 5 or parts[-1] != "info" or parts[-3] != "acls":
            raise AssertionError(f"FakeHost does not know file: {path}")
        target, initiator = parts[-5], parts[-2]
        if initiator not in self.targets.get(target, {}).get("acls", {}):
            return self._err(f"cat: {path}: No such file or directory")
        if initiator in self.sessions:
            return self._ok(f"InitiatorName: {initiator}\nSession State: TARG_SESS_STATE_LOGGED_IN")
        return self._ok(f"No active iSCSI Session for Initiator Endpoint: {initiator}")

    # ── targetcli ─────────────────────────────────────────────────────

    def visible(self, initiator: str) -> list[str]:
        """Devices an initiator sees, by mapped LUN number, across all targets."""
        out = []
        for t in self.targets.values():
            mapped = t["acls"].get(initiator, {})
            out += [self.backstores[t["luns"][tl]] for _, tl in sorted(mapped.items())]
        return out

    def _drop_backstore(self, name: str) -> None:
        """Like rtslib: deleting a storage object deletes its LUNs and their mappings."""
        del self.backstores[name]
        for t in self.targets.values():
            for idx in [i for i, b in t["luns"].items() if b == name]:
                del t["luns"][idx]
                for mapped in t["acls"].values():
                    for ml in [m for m, tl in mapped.items() if tl == idx]:
                        del mapped[ml]

    @staticmethod
    def _kw(args: list[str]) -> dict[str, str]:
        return dict(x.split("=", 1) for x in args if "=" in x)

    def _ls(self, header: str, names) -> tuple:
        lines = [f"o- {header}"] + [f"  o- {n} ........ [...]" for n in names]
        return self._ok("\n".join(lines))

    def _targetcli(self, a: list[str]):
        if a == ["saveconfig"]:
            return self._ok()
        path, verb, args = a[0], a[1], a[2:]

        if path == "/backstores/block":
            if verb == "ls":
                return self._ls("block", sorted(self.backstores))
            if verb == "create":
                kw = self._kw(args)
                name, dev = kw["name"], kw["dev"]
                if name in self.backstores:
                    return self._err(f"Storage object block/{name} exists")
                if dev.removeprefix("/dev/zvol/") not in self.datasets:
                    return self._err(f"Device {dev} does not exist")
                self.backstores[name] = dev
                return self._ok()
            if verb == "delete":
                if args[0] not in self.backstores:
                    return self._err(f"No storage object named {args[0]}")
                self._drop_backstore(args[0])
                return self._ok()

        if path == "/iscsi":
            if verb == "ls":
                return self._ls(f"iscsi .... [Targets: {len(self.targets)}]", sorted(self.targets))
            if verb == "create":
                if args[0] in self.targets:
                    return self._err("This Target already exists in configFS")
                # LIO's default: a new target gets a 0.0.0.0:3260 portal.
                self.targets[args[0]] = {"luns": {}, "acls": {}, "portals": {"0.0.0.0:3260"},
                                         "attrs": {}}
                return self._ok()
            if verb == "delete":
                if args[0] not in self.targets:
                    return self._err("No such Target in configfs")
                del self.targets[args[0]]
                return self._ok()

        parts = path.split("/")       # ["", "iscsi", iqn, "tpg1", sub, (acl)]
        if len(parts) >= 4 and parts[1] == "iscsi" and parts[3] == "tpg1":
            t = self.targets.get(parts[2])
            if t is None:
                return self._err(f"No such path {path}")
            sub = parts[4] if len(parts) > 4 else None

            if sub is None and verb == "set":
                t["attrs"].update(self._kw(args[1:]))
                return self._ok()

            if sub == "portals":
                portal = f"{args[0]}:{args[1]}" if args else ""
                if verb == "create":
                    t["portals"].add(portal)
                    return self._ok()
                if verb == "delete":
                    if portal not in t["portals"]:
                        return self._err("No such NetworkPortal in configfs")
                    t["portals"].discard(portal)
                    return self._ok()
                if verb == "ls":
                    return self._ls("portals", sorted(t["portals"]))

            if sub == "luns":
                if verb == "ls":
                    lines = ["o- luns"] + [f"  o- lun{i} .... [block/{b} ({self.backstores[b]})]"
                                           for i, b in sorted(t["luns"].items())]
                    return self._ok("\n".join(lines))
                if verb == "create":
                    name = args[0].removeprefix("/backstores/block/")
                    kw = self._kw(args[1:])
                    idx = int(kw["lun"])
                    if name not in self.backstores:
                        return self._err(f"No storage object {args[0]}")
                    if idx in t["luns"]:
                        return self._err(f"LUN {idx} already exists")
                    t["luns"][idx] = name
                    # targetcli default auto_add_mapped_luns=true: map into every ACL.
                    if kw.get("add_mapped_luns", "true") != "false":
                        for mapped in t["acls"].values():
                            mapped[max(mapped, default=-1) + 1] = idx
                    return self._ok()

            if sub == "acls" and len(parts) == 5:
                if verb == "create":
                    wwn = args[0]
                    if wwn in t["acls"]:
                        return self._err(f"This NodeACL already exists in configFS")
                    t["acls"][wwn] = {}
                    if self._kw(args[1:]).get("add_mapped_luns", "true") != "false":
                        t["acls"][wwn] = dict(enumerate(sorted(t["luns"])))
                    return self._ok()
                if verb == "delete":
                    if args[0] not in t["acls"]:
                        return self._err("No such NodeACL in configfs")
                    del t["acls"][args[0]]
                    return self._ok()

            if sub == "acls" and len(parts) == 6 and verb == "create":
                mapped = t["acls"].get(parts[5])
                if mapped is None:
                    return self._err(f"No such path {path}")
                kw = self._kw(args)
                ml, tl = int(kw["mapped_lun"]), int(kw["tpg_lun_or_backstore"].removeprefix("lun"))
                if tl not in t["luns"]:
                    return self._err(f"No such LUN lun{tl}")
                if ml in mapped:
                    return self._err(f"Mapped LUN {ml} already exists")
                mapped[ml] = tl
                return self._ok()

        raise AssertionError(f"FakeHost does not know targetcli command: {a}")
