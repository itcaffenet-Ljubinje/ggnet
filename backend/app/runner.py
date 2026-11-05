"""CommandRunner: runs privileged commands (zfs, zpool, targetcli) on the host.

ggNet runs directly on the Proxmox host as root (see scripts/install_ggnet.sh),
so commands are executed locally, without ssh or sudo. Managers accept any
object with the same `run()` signature, which is how tests inject a fake.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from typing import Callable, Optional

logger = logging.getLogger("ggnet.exec")

DEFAULT_TIMEOUT = 60
_PROGRESS_LINE = re.compile(r"^(\d|full\t|size\t|incremental\t)")


class CommandRunner:
    """Thin command executor; knows nothing about ZFS or iSCSI."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        # stderr of the last FAILED command, for API error messages,
        # since managers only return bool.
        self.last_error: str = ""

    def run(
        self,
        cmd: list[str],
        timeout: Optional[int] = None,
        quiet: bool = False,
    ) -> tuple[bool, str, str]:
        """
        Run a command. Returns (success, stdout, stderr).

        quiet=True suppresses error logging, for checks where failure is an
        expected outcome (e.g. "does this dataset exist"), not a real error.
        """
        timeout = timeout or self.timeout
        str_cmd = [str(c) for c in cmd]

        try:
            result = subprocess.run(
                str_cmd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            logger.error("Command timed out (%ss): %s", timeout, " ".join(str_cmd))
            self.last_error = f"timeout: {' '.join(str_cmd)}"
            return False, "", "timeout"
        except FileNotFoundError as e:
            logger.error("Executable not found: %s", e)
            self.last_error = str(e)
            return False, "", str(e)

        ok = result.returncode == 0
        if not ok:
            self.last_error = result.stderr.strip()
            if not quiet:
                logger.error(
                    "Command failed [%s]: %s", " ".join(str_cmd), result.stderr.strip()
                )
        return ok, result.stdout.strip(), result.stderr.strip()

    def run_stream(
        self,
        cmd: list[str],
        on_output: Callable[[str], None],
        timeout: Optional[int] = None,
        cancel: Optional[threading.Event] = None,
    ) -> tuple[bool, str, str]:
        """
        Run a long command and hand each piece of its stdout to `on_output` as
        it comes (split on newlines and carriage returns, as progress bars
        like `qemu-img convert -p` print). Setting `cancel` stops it.
        Returns (success, "", stderr).
        """
        timeout = timeout or self.timeout
        str_cmd = [str(c) for c in cmd]
        try:
            proc = subprocess.Popen(str_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            self.last_error = str(e)
            return False, "", str(e)
        err_lines: list[bytes] = []
        drain = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
        drain.start()
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        if cancel is not None:
            def watch() -> None:
                while proc.poll() is None:
                    if cancel.wait(0.5):
                        proc.kill()
                        return
            threading.Thread(target=watch, daemon=True).start()
        try:
            buf = b""
            while chunk := proc.stdout.read1(4096):
                buf += chunk
                *pieces, buf = re.split(rb"[\r\n]", buf)
                for piece in pieces:
                    if piece.strip():
                        try:
                            on_output(piece.decode(errors="replace").strip())
                        except Exception:     # a progress callback never breaks the command
                            logger.exception("Output callback failed")
            proc.wait()
        finally:
            timer.cancel()
        drain.join(timeout=10)
        err = b"".join(err_lines).decode(errors="replace").strip()
        if proc.returncode != 0:
            self.last_error = err or ("timeout" if proc.returncode == -9 else f"exit code {proc.returncode}")
            logger.error("Command failed [%s]: %s", " ".join(str_cmd), self.last_error)
            return False, "", self.last_error
        return True, "", err

    def run_pipe(
        self,
        producer: list[str],
        consumer: list[str],
        timeout: Optional[int] = None,
        on_producer_line: Optional[Callable[[str], None]] = None,
    ) -> tuple[bool, str, str]:
        """
        Run `producer | consumer` without a shell (e.g. zfs send | zfs recv).
        Succeeds only if BOTH commands exit 0. Returns (success, stdout of the
        consumer, stderr of whichever failed). `on_producer_line` gets each
        stderr line of the producer as it comes (progress of `zfs send -v`).
        """
        timeout = timeout or self.timeout
        p_cmd = [str(c) for c in producer]
        c_cmd = [str(c) for c in consumer]
        shown = f"{' '.join(p_cmd)} | {' '.join(c_cmd)}"
        try:
            prod = subprocess.Popen(p_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as e:
            self.last_error = str(e)
            return False, "", str(e)
        prod_lines: list[str] = []

        def read_stderr() -> None:
            for raw in prod.stderr:
                line = raw.decode(errors="replace").rstrip("\n")
                prod_lines.append(line)
                if on_producer_line is not None:
                    try:
                        on_producer_line(line)
                    except Exception:     # a progress callback never breaks the copy
                        logger.exception("Progress callback failed")

        reader = threading.Thread(target=read_stderr, daemon=True) if prod.stderr else None
        if reader:
            reader.start()
        try:
            cons = subprocess.run(
                c_cmd, stdin=prod.stdout, capture_output=True, text=True, timeout=timeout
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            prod.kill()
            prod.wait()
            err = "timeout" if isinstance(e, subprocess.TimeoutExpired) else str(e)
            logger.error("Pipe failed (%s): %s", err, shown)
            self.last_error = f"{err}: {shown}"
            return False, "", err
        finally:
            if prod.stdout:
                prod.stdout.close()
        prod.wait()
        if reader:
            reader.join(timeout=10)
        # `zfs send -Pv` size and progress lines are noise in an error message.
        prod_err = "\n".join(line for line in prod_lines
                             if line and not _PROGRESS_LINE.match(line)).strip()

        if prod.returncode != 0 or cons.returncode != 0:
            # When the consumer fails first, the producer dies of SIGPIPE with
            # no message of its own; the consumer's stderr is the real reason.
            errors = [prod_err, cons.stderr.strip()] if prod.returncode not in (0, -13) \
                else [cons.stderr.strip(), prod_err]
            err = next((e for e in errors if e), "") \
                or f"exit code {prod.returncode}/{cons.returncode}"
            self.last_error = err
            logger.error("Pipe failed [%s]: %s", shown, err)
            return False, "", err
        return True, cons.stdout.strip(), ""
