"""Flash through the custom recovery (route B): copy an OPxFlasher recovery zip to the phone, install it in AERA.

The other way to flash (route A) runs every write over USB from the PC (fastboot / fastbootd), where a flaky
cable or Windows driver can stop a flash half way. Here the cable only carries a copy:

  1. the recovery zip is made (it keeps the custom recovery, so a failed install can simply be retried),
  2. copied to the phone's internal storage while Android runs, and its SHA-256 checked on the phone,
  3. the phone restarts into the custom recovery (AERA), and the owner installs the zip there
     (Install > OPxFlasher > the zip > swipe): the phone reads and writes everything itself,
  4. the installer leaves one line in /tmp (rom-install.sh: RESULT), which OPxFlasher reads over adb.

The same variant only: a region change needs data formatted, which a zip read from internal storage (part of
data) can't do. That stays with the PC route. OPxFlasher never downloads or ships the recovery itself: the owner
installs it from its official page (opx.customrec).
"""

from __future__ import annotations

import hashlib
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opx.tools import Tool

PHONE_DIR = "/sdcard/OPxFlasher"
RESULT = "/tmp/opx-install.result"
INSTALL_LOG = "/tmp/opx-install.log"
RECOVERY_LOG = "/tmp/recovery.log"
SPARE = 512 * 1024 * 1024  # leave this much free on the phone after the copy

Event = dict
EventFn = Callable[[Event], None]


class InRecoveryError(RuntimeError):
    pass


NO_RECOVERY = (
    "The phone didn't come back in a custom recovery with USB (adb). If it shows the stock recovery, AERA isn't "
    "installed: install it first (Single images > Install a custom recovery (AERA)...), then try again. If AERA "
    "is on the screen, unplug and replug the cable and press Resume.")
DROPPED = ("The phone stopped answering during the copy. Plug it straight into the PC (a rear / motherboard "
           "port, not the case's front port) and start again: the copy restarts, nothing was written.")


@dataclass
class Options:
    hardware: str = ""  # "cn" / "global" when the ROM needs it (Android 17 on a OnePlus 13)
    reboot_after: bool = True
    delete_after: bool = True  # remove the zip from the phone after a good install (it's ~10 GB)


@dataclass
class Outcome:
    zip_path: Path
    phone_path: str
    result: str
    log: Path | None = None
    notes: list[str] = field(default_factory=list)
    rebooted: bool = False

    @property
    def needs_format(self) -> bool:
        return self.result == "ok format-data"


STEPS = ["Make the recovery zip", "Copy it to the phone", "Check the copy on the phone",
         "Restart into the custom recovery", "Install it in the custom recovery", "Finish"]


def zip_keeps_recovery(path: Path) -> tuple[bool, str]:
    """(it's an OPxFlasher recovery zip that leaves the custom recovery alone, its cnkeep line)."""
    try:
        with zipfile.ZipFile(path) as zf:
            lines = zf.read("opx/rom.list").decode("utf-8", "replace").splitlines()
    except (OSError, KeyError, zipfile.BadZipFile):
        return False, ""
    cnkeep = " ".join(line[len("cnkeep "):] for line in lines if line.startswith("cnkeep "))
    return "keeprecovery" in lines, cnkeep


def sha256_file(path: Path, cancel: Callable[[], bool] = lambda: False) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(4 << 20), b""):
            if cancel():
                raise InRecoveryError("Stopped. Nothing was written.")
            h.update(block)
    return h.hexdigest()


def _states(adb: Tool, serial: str) -> str:
    """The phone's adb state ("device", "recovery", "sideload", ...) or "" when adb doesn't see it."""
    res = adb.run("devices", timeout=15)
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial:
            return parts[1]
    return ""


def _free_bytes(df_text: str) -> int | None:
    """Available bytes from `df -k <dir>` (toybox: Filesystem 1K-blocks Used Available Use% Mounted on)."""
    lines = [ln for ln in df_text.strip().splitlines() if ln.strip()]
    # one filesystem; a long filesystem name can wrap the row onto two lines
    row = " ".join(lines[1:]).split()
    try:
        return int(row[3]) * 1024
    except (IndexError, ValueError):
        return None


class Runner:
    def __init__(self, rom, adb: Tool, serial: str, out_dir: Path, workdir: Path, options: Options | None = None,
                 on_event: EventFn | None = None, cancel: threading.Event | None = None,
                 recovery_timeout: float = 180, install_timeout: float = 3600, poll: float = 3):
        self.rom = rom
        self.adb = adb.bind(serial)
        self.adb_all = adb
        self.serial = serial
        self.out_dir = Path(out_dir)
        self.workdir = Path(workdir)
        self.options = options or Options()
        self.emit = on_event or (lambda e: None)
        self.cancel = cancel or threading.Event()
        self.recovery_timeout = recovery_timeout
        self.install_timeout = install_timeout
        self.poll = poll

    # --- helpers ---------------------------------------------------------------------------

    def _step(self, index: int, detail: str = "") -> None:
        self.emit({"type": "step_start", "index": index, "total": len(STEPS), "label": STEPS[index - 1]})
        if detail:
            self.emit({"type": "log", "message": f"    {detail}"})

    def _done(self, detail: str) -> None:
        self.emit({"type": "step_done", "status": "done", "detail": detail})

    def _on_phone(self, message: str) -> None:
        """Something the owner does on the phone's screen."""
        self.emit({"type": "on_phone", "message": message})

    def _stopped(self) -> bool:
        return self.cancel.is_set()

    def _check_stop(self, written: bool = False) -> None:
        if self._stopped():
            raise InRecoveryError("Stopped." + ("" if written else " Nothing was written."))

    def _sleep(self, seconds: float) -> None:
        self.cancel.wait(seconds)

    def _shell(self, cmd: str, timeout: float = 60):
        return self.adb.run("shell", cmd, timeout=timeout)

    def _wait_state(self, wanted: tuple[str, ...], timeout: float) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._check_stop()
            state = _states(self.adb_all, self.serial)
            if state in wanted:
                return state
            self._sleep(self.poll)
        return ""

    # --- 1. the zip ------------------------------------------------------------------------

    def make_zip(self) -> Path:
        from opx import convert as cv

        path = Path(self.rom.path)
        if self.rom.kind == "fullrom" and path.suffix.lower() == ".zip":
            keeps, cnkeep = zip_keeps_recovery(path)
            if keeps and (bool(cnkeep) == (self.options.hardware == "cn") or not cv.hardware_needed(self.rom)):
                self._done(f"using {path.name} (it keeps your custom recovery)")
                return path
        stage = (lambda s: self.emit({"type": "log", "message": f"    {s}"}))

        def progress(done, total):
            if total:
                self.emit({"type": "transfer", "what": "Making the recovery zip", "done": done, "total": total})

        try:
            made = cv.convert_or_reuse(self.rom, self.out_dir, make_zip=True, make_folder=False, progress=progress,
                                       stage=stage, cancel=self._stopped, keep_recovery_zip=True,
                                       hardware=self.options.hardware)
        except cv.ConvertError as e:
            raise InRecoveryError(str(e)) from e
        zip_path = made["zip"]
        self._done(f"{zip_path.name} (keeps your custom recovery)")
        return zip_path

    # --- 2./3. copy and check ----------------------------------------------------------------

    def _remote_size(self, remote: str) -> int | None:
        res = self._shell(f"stat -c %s '{remote}'", timeout=20)
        try:
            return int(res.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return None

    def _remote_sha(self, remote: str) -> str:
        res = self._shell(f"sha256sum '{remote}'", timeout=900)
        word = (res.stdout.split() or [""])[0].lower()
        return word if len(word) == 64 else ""

    def copy(self, zip_path: Path, sha: str) -> str:
        remote = f"{PHONE_DIR}/{zip_path.name}"
        size = zip_path.stat().st_size
        if self._remote_size(remote) == size and self._remote_sha(remote) == sha:
            self._done("already on the phone (same SHA-256): not copied again")
            return remote
        free = _free_bytes(self._shell("df -k /sdcard", timeout=20).stdout)
        if free is not None and free < size + SPARE:
            raise InRecoveryError(
                f"Not enough free space on the phone: the zip needs {size / 1e9:.1f} GB (+0.5 GB spare), "
                f"{free / 1e9:.1f} GB is free. Free some space, or flash from the PC.")
        self._shell(f"mkdir -p '{PHONE_DIR}'", timeout=20)
        for attempt in (1, 2):
            if self._push(zip_path, remote, size):
                self._done(f"{size / 1e9:.1f} GB copied to {remote}")
                return remote
            self._shell(f"rm -f '{remote}'", timeout=20)
            if attempt == 1:
                self.emit({"type": "log", "message": "    The copy stopped; waiting for the phone to copy again..."})
                if not self._wait_state(("device",), 120):
                    break
        raise InRecoveryError(DROPPED)

    def _push(self, local: Path, remote: str, size: int) -> bool:
        proc = self.adb.popen("push", str(local), remote)
        try:
            while proc.poll() is None:
                if self._stopped():
                    proc.kill()
                    raise InRecoveryError("Stopped. Nothing was written (the copy on the phone is incomplete).")
                done = self._remote_size(remote) or 0
                self.emit({"type": "transfer", "what": "Copying to the phone", "done": min(done, size),
                           "total": size})
                self._sleep(self.poll)
            out, err = proc.communicate()
        finally:
            if proc.poll() is None:
                proc.kill()
        if proc.returncode != 0:
            text = (err or out or b"").decode("utf-8", "replace").strip()
            self.emit({"type": "log", "message": f"    adb push: {text.splitlines()[-1] if text else proc.returncode}"})
            return False
        self.emit({"type": "transfer", "what": "Copying to the phone", "done": size, "total": size})
        return True

    def verify(self, remote: str, sha: str) -> None:
        got = self._remote_sha(remote)
        if not got:
            raise InRecoveryError("The phone couldn't check the copy (no sha256sum). Flash from the PC instead.")
        if got != sha:
            self._shell(f"rm -f '{remote}'", timeout=20)
            raise InRecoveryError("The copy on the phone is damaged (its SHA-256 differs). It was deleted; start "
                                  "again, and use a rear / motherboard USB port. Nothing was written.")
        self._done(f"SHA-256 matches ({sha[:12]}...)")

    # --- 4./5. recovery and install --------------------------------------------------------

    def to_recovery(self) -> None:
        state = _states(self.adb_all, self.serial)
        if state != "recovery":
            res = self.adb.run("reboot", "recovery", timeout=30)
            if res.returncode != 0 and "closed" not in (res.stderr + res.stdout).lower():
                raise InRecoveryError(f"The phone didn't restart into recovery: {res.stderr.strip() or res.stdout}")
            self._sleep(min(5, self.poll * 2))
        if not self._wait_state(("recovery",), self.recovery_timeout):
            raise InRecoveryError(NO_RECOVERY)
        self._done("the phone is in the custom recovery")

    def _visible(self, remote: str) -> bool:
        res = self._shell(f"ls '{remote}'", timeout=20)
        return res.returncode == 0 and remote in res.stdout

    def _result(self) -> str:
        res = self._shell(f"cat {RESULT}", timeout=20)
        return res.stdout.strip() if res.returncode == 0 else ""

    def install(self, remote: str) -> str:
        if not self._visible(remote):
            self._on_phone("Unlock the custom recovery (your PIN / pattern) so it can read internal storage.")
            end = time.monotonic() + 600
            while not self._visible(remote):
                self._check_stop()
                if time.monotonic() > end:
                    raise InRecoveryError("The custom recovery can't read internal storage (unlock it with your "
                                          "PIN / pattern). Nothing was written.")
                self._sleep(self.poll)
        self._shell(f"rm -f {RESULT}", timeout=20)
        name = remote.rsplit("/", 1)[1]
        self._on_phone(f"On the phone: Install > Internal storage > OPxFlasher > {name} > swipe to confirm. "
                       "OPxFlasher waits here and reads the result.")
        end = time.monotonic() + self.install_timeout
        started = False
        while True:
            if self._stopped():
                raise InRecoveryError("Stopped waiting. If the install is running on the phone, let it finish "
                                      "there before rebooting.")
            state = _states(self.adb_all, self.serial)
            result = self._result() if state == "recovery" else ""
            if result == "running" and not started:
                started = True
                self.emit({"type": "log", "message": "    Installing on the phone..."})
            if result.startswith(("ok", "fail")):
                break
            if time.monotonic() > end:
                raise InRecoveryError("No result from the install yet. Check the phone's screen: if it says "
                                      "Done, reboot it from the recovery.")
            self._sleep(self.poll)
        self.emit({"type": "on_phone", "message": ""})
        return result

    def save_logs(self) -> Path | None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        saved = None
        for remote, local in ((INSTALL_LOG, "opx-install.log"), (RECOVERY_LOG, "recovery.log")):
            res = self.adb.run("pull", remote, str(self.workdir / local), timeout=60)
            if res.returncode == 0 and saved is None:
                saved = self.workdir / local
        return saved

    # --- all -------------------------------------------------------------------------------

    def run(self) -> Outcome:
        if _states(self.adb_all, self.serial) not in ("device", "recovery"):
            raise InRecoveryError("Connect the phone in Android (USB debugging on) or in the custom recovery.")
        self._step(1)
        zip_path = self.make_zip()
        self._check_stop()
        self.emit({"type": "log", "message": "    SHA-256 of the zip..."})
        sha = sha256_file(zip_path, self._stopped)
        self._step(2, f"{zip_path.name} -> {PHONE_DIR}")
        remote = self.copy(zip_path, sha)
        self._step(3)
        self.verify(remote, sha)
        self._check_stop()
        self._step(4)
        self.to_recovery()
        self._step(5)
        result = self.install(remote)
        log = self.save_logs()
        outcome = Outcome(zip_path, remote, result, log)
        if result.startswith("fail"):
            kind, _, why = result[len("fail "):].partition(": ")
            where = f" The install log is saved: {log}" if log else ""
            if kind == "nothing-written":
                raise InRecoveryError(f"The install stopped before writing anything: {why}{where}")
            raise InRecoveryError(f"The install failed part way: {why} Do NOT reboot: install the zip again in "
                                  f"the recovery (it's still on the phone), or flash from the PC.{where}")
        self._step(6)
        if self.options.delete_after:
            self._shell(f"rm -f '{remote}'", timeout=30)
            outcome.notes.append(f"Deleted {remote} from the phone.")
        if outcome.needs_format:
            outcome.notes.append("ONE MORE STEP on the phone: Wipe > Format data > type yes, then reboot.")
        elif self.options.reboot_after:
            self.adb.run("reboot", timeout=30)
            outcome.rebooted = True
            outcome.notes.append("The phone is restarting. The first boot can take a few minutes.")
        self._done("installed" + (": " + " ".join(outcome.notes) if outcome.notes else ""))
        self.emit({"type": "finished"})
        return outcome
