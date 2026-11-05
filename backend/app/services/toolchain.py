"""
Add new hardware: an OS image learns a PC's network card the way Windows
learns any new device, by installing it.

A PC whose boot network card the image has never had installed stops at
0x7B: Windows loads the boot card's driver before Plug and Play runs only
when the card's device entry (under its exact instance ID) is configured.
That entry is the same on every PC with the same board, so it is added
once per board type, by the Add new hardware wizard (HardwareWizard). The
server

  1. clones the image's newest version and writes into the clone the agent,
     its service and a job naming the cards (PCI hardware ID + instance ID,
     as the PC's agent or the hardware scan reports them);
  2. boots the clone in a temporary Proxmox VM (ToolchainVm: OVMF, the
     clone as its SATA disk, no network). The agent finds the job, creates
     each card's device under its instance ID with SetupAPI and lets
     Windows install its best driver (inbox or from the driver store),
     writes the result and shuts the VM down;
  3. reads the result, prepares the clone again (the new card's driver is
     marked for a network boot) and makes it the image's next version.

The PCs keep booting the current version while this runs; the VM is
removed when it ends, whatever the outcome. HardwareAdder runs one job per
image in the background and keeps its progress in memory for the UI.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.services.provisioning import Provisioner, ProvisioningError
from app.windows_setup import ImagePrep

logger = logging.getLogger("ggnet.toolchain")

VM_NAME_PREFIX = "ggnet-hw-"
VM_TAG = "ggnet-toolchain"
VM_MEMORY_MB = 4096
VM_CORES = 4
# Windows' first start on the VM's devices plus the install; then it is stuck.
VM_TIMEOUT = 30 * 60
POLL_EVERY = 5.0
_STATUS = re.compile(r"^status:\s*(\w+)", re.MULTILINE)


class ToolchainVm:
    """A temporary Proxmox VM that boots one zvol until Windows shuts down."""

    def __init__(self, runner, timeout: float = VM_TIMEOUT, poll: float = POLL_EVERY,
                 sleep: Callable[[float], None] = time.sleep):
        self.runner = runner
        self.timeout = timeout
        self.poll = poll
        self.sleep = sleep

    def next_id(self) -> int | None:
        ok, out, _ = self.runner.run(["pvesh", "get", "/cluster/nextid"])
        try:
            return int(out.strip().strip('"')) if ok else None
        except ValueError:
            return None

    def create_args(self, vmid: int, device: str, label: str) -> list[str]:
        return [
            "qm", "create", str(vmid), "--name", f"{VM_NAME_PREFIX}{label}"[:63],
            "--description", "ggNet Add new hardware: temporary, removed when the run ends",
            "--tags", VM_TAG, "--ostype", "win11", "--machine", "q35", "--bios", "ovmf",
            "--cpu", "host", "--cores", str(VM_CORES), "--memory", str(VM_MEMORY_MB),
            "--sata0", f"{device},cache=none", "--boot", "order=sata0", "--vga", "std",
        ]

    def status(self, vmid: int) -> str | None:
        ok, out, _ = self.runner.run(["qm", "status", str(vmid)], quiet=True)
        m = _STATUS.search(out) if ok else None
        return m.group(1) if m else None

    def remove(self, vmid: int) -> None:
        """Stop the VM and remove it; its disk (the clone) is only detached."""
        if self.status(vmid) == "running":
            self.runner.run(["qm", "stop", str(vmid), "--skiplock", "1"])
        self.runner.run(["qm", "set", str(vmid), "--delete", "sata0"])
        if not self.runner.run(["qm", "destroy", str(vmid), "--skiplock", "1"])[0]:
            logger.error("Could not remove the toolchain VM %s: %s", vmid, self.runner.last_error)

    def run(self, device: str, label: str, cancel: threading.Event | None = None) -> str | None:
        """Boot `device` and wait until the VM stops by itself. None, or why it failed."""
        vmid = self.next_id()
        if vmid is None:
            return f"no free VM ID: {self.runner.last_error}"
        if not self.runner.run(self.create_args(vmid, device, label))[0]:
            return f"creating the toolchain VM failed: {self.runner.last_error}"
        try:
            if not self.runner.run(["qm", "start", str(vmid)])[0]:
                return f"starting the toolchain VM failed: {self.runner.last_error}"
            logger.info("Toolchain VM %s started on %s", vmid, device)
            waited = 0.0
            while waited < self.timeout:
                if cancel is not None and cancel.is_set():
                    return "cancelled"
                self.sleep(self.poll)
                waited += self.poll
                status = self.status(vmid)
                if status == "stopped":
                    logger.info("Toolchain VM %s stopped after %.0f s", vmid, waited)
                    return None
                if status is None:
                    return f"the toolchain VM {vmid} is gone"
            return f"Windows did not finish within {self.timeout // 60:.0f} minutes"
        finally:
            self.remove(vmid)


@dataclass
class HardwareJob:
    nics: list[tuple[str, str]]
    step: str = "starting"
    error: str | None = None
    snapshot: str | None = None
    devices: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def running(self) -> bool:
        return self.finished is None

    def out(self) -> dict:
        return {"cards": [{"nic": n, "instance": i} for n, i in self.nics], "step": self.step,
                "running": self.running, "error": self.error, "snapshot": self.snapshot,
                "devices": self.devices, "started": self.started, "finished": self.finished}


def card_name(devices: list[dict], nic: str, instance: str) -> str:
    """The model of a card as the agent reported it (the driver's device name)."""
    wanted = f"PCI\\{nic}\\{instance}".upper()
    for d in devices:
        if str(d.get("instance_id") or "").upper() == wanted:
            return str(d.get("name") or d.get("driver") or nic)[:255]
    return nic


def activate_when_done(make_session, disk_id: int, base: str) -> Callable[[str, "HardwareJob"], None]:
    """
    A HardwareAdder `done` callback: the image records the cards it now has
    (app.db.models.ImageHardware), and the new version becomes its active
    one, unless someone chose another version meanwhile.
    """
    from app.db.models import GameDisk, ImageHardware

    def done(snapshot: str, job: "HardwareJob") -> None:
        with make_session() as s:
            d = s.get(GameDisk, disk_id)
            if d is None:
                return
            have = {(h.nic, h.instance) for h in d.hardware}
            for nic, instance in job.nics:
                if (nic, instance) not in have:
                    d.hardware.append(ImageHardware(nic=nic, instance=instance, snapshot=snapshot,
                                                    name=card_name(job.devices, nic, instance)))
            if d.snapshot == base:
                d.snapshot = snapshot
            s.commit()

    return done


# How long the wizard waits for the new PC to boot.
WIZARD_TIMEOUT = 60 * 60


@dataclass
class HardwareWizard:
    """
    The Add new hardware wizard of one image: the admin starts it, then
    powers on the new PC. iPXE sends that PC to the hardware scan (or, when
    its card is known, straight to the install), the job adds its card to a
    new version, and the PC boots it (app.api.v1.boot).

    step: waiting (for a PC to boot) → scanning (the PC is in Windows PE) →
    collected (the scan reported its card) → adding (the toolchain job runs)
    → rebooting (the new version is active; waiting for the PC to boot it)
    → done; or failed.
    """

    disk_id: int
    base: str
    step: str = "waiting"
    machine: str | None = None
    card: tuple[str, str] | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None

    @property
    def active(self) -> bool:
        return self.finished is None

    def fail(self, error: str) -> None:
        self.step, self.error, self.finished = "failed", error, time.time()

    def out(self) -> dict:
        return {"step": self.step, "active": self.active, "machine": self.machine,
                "card": {"nic": self.card[0], "instance": self.card[1]} if self.card else None,
                "error": self.error, "started": self.started, "finished": self.finished}


class HardwareAdder:
    """Add new hardware jobs and wizards, one per image, in the background."""

    def __init__(self, prov: Provisioner, vm: ToolchainVm, prep: Callable[[], ImagePrep],
                 background: bool = True):
        self.prov = prov
        self.vm = vm
        self.prep = prep
        self.background = background     # tests run the job in the request
        self.jobs: dict[str, HardwareJob] = {}
        self.wizards: dict[str, HardwareWizard] = {}
        # PCs that wait on screen for the wizard: image → {machine: last iPXE request}.
        self.waiting: dict[str, dict[str, float]] = {}
        self._guard = threading.Lock()

    def note_waiting(self, master: str, machine: str) -> None:
        self.waiting.setdefault(master, {})[machine] = time.time()

    def waiting_machines(self, master: str) -> list[str]:
        """PCs that asked iPXE in the last minute (they ask every 10 s while waiting)."""
        seen = self.waiting.get(master, {})
        return sorted(m for m, t in seen.items() if time.time() - t < 60)

    def job(self, master: str) -> HardwareJob | None:
        return self.jobs.get(master)

    def wizard(self, master: str) -> HardwareWizard | None:
        wizard = self.wizards.get(master)
        if (wizard is not None and wizard.active and wizard.step == "waiting"
                and time.time() - wizard.started > WIZARD_TIMEOUT):
            wizard.fail(f"no PC booted within {WIZARD_TIMEOUT // 60} minutes")
        return wizard

    def start_wizard(self, master: str, disk_id: int, base: str) -> HardwareWizard:
        """ValueError when a wizard or job already runs for the image."""
        with self._guard:
            current = self.wizard(master)
            if current is not None and current.active:
                raise ValueError("Add new hardware is already open for this image")
            job = self.jobs.get(master)
            if job is not None and job.running:
                raise ValueError("Add new hardware is already running for this image")
            wizard = self.wizards[master] = HardwareWizard(disk_id=disk_id, base=base)
        logger.info("Add new hardware wizard opened for %s", master)
        return wizard

    def close_wizard(self, master: str) -> bool:
        """Finish (or cancel) the wizard; a running job is cancelled too."""
        wizard = self.wizards.pop(master, None)
        cancelled = self.cancel(master)
        if wizard is not None and wizard.active:
            wizard.fail("cancelled")
        return wizard is not None or cancelled

    def start(self, master: str, nics: list[tuple[str, str]],
              done: Callable[[str, HardwareJob], None] = lambda snapshot, job: None) -> HardwareJob:
        """Start a job; `done(new snapshot, job)` runs when it succeeded. ValueError when one runs."""
        with self._guard:
            current = self.jobs.get(master)
            if current is not None and current.running:
                raise ValueError("Add new hardware is already running for this image")
            job = self.jobs[master] = HardwareJob(nics=list(nics))
        if self.background:
            threading.Thread(target=self._run, args=(master, job, done), name="ggnet-hwadd", daemon=True).start()
        else:
            self._run(master, job, done)
        return job

    def cancel(self, master: str) -> bool:
        job = self.jobs.get(master)
        if job is None or not job.running:
            return False
        job.cancel.set()
        return True

    def _run(self, master: str, job: HardwareJob, done: Callable[[str, HardwareJob], None]) -> None:
        label = master.rsplit("/", 1)[-1]

        def step(name: str) -> None:
            if job.cancel.is_set():
                raise ProvisioningError("cancelled")
            job.step = name

        try:
            snapshot, devices = self.prov.add_hardware(
                master, job.nics, self.prep(), boot=lambda device: self.vm.run(device, label, job.cancel), step=step)
            job.snapshot, job.devices, job.step = snapshot, devices, "done"
            try:
                comment = "ggNet: added " + ", ".join(f"PCI\\{n}" for n, _ in job.nics)
                self.prov.set_comment(master, snapshot, comment[:200])
            except Exception as e:      # the version is there; only its comment is missing
                logger.warning("%s", e)
            done(snapshot, job)
            logger.info("Add new hardware on %s done: @%s", master, snapshot)
        except ProvisioningError as e:
            job.error = str(e)
            logger.warning("Add new hardware on %s failed: %s", master, e)
        except Exception as e:
            job.error = f"unexpected error: {e}"
            logger.exception("Add new hardware on %s failed", master)
        finally:
            job.finished = time.time()
            wizard = self.wizards.get(master)
            if wizard is not None and wizard.active and wizard.card in job.nics:
                if job.error:
                    wizard.fail(job.error)
                else:
                    wizard.step = "rebooting"
