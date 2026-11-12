"""Talking to the bootloader (fastboot) and fastbootd.

This is the only module that sends commands which change the phone. Every
write goes through :class:`Fastboot`, which refuses partitions on the
profile's never-flash list and validates names before building a command.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from opx.device import parse_fastboot_devices, parse_getvar_all
from opx.profile import Profile
from opx.remote import check_name
from opx.tools import Tool


class FastbootError(RuntimeError):
    pass


class RefusedPartition(FastbootError):
    pass


def poll_interval() -> float:
    try:
        return float(os.environ.get("OPX_POLL_SECONDS", "1"))
    except ValueError:
        return 1.0


class Fastboot:
    def __init__(self, tool: Tool, profile: Profile):
        if not tool.serial:
            raise ValueError("Fastboot needs a tool bound to one serial")
        self.tool = tool
        self.profile = profile

    # --- reading ------------------------------------------------------

    def getvar(self, name: str) -> str | None:
        res = self.tool.run("getvar", name, timeout=20)
        for line in (res.stderr + "\n" + res.stdout).splitlines():
            line = line.strip()
            if line.startswith("(bootloader) "):
                line = line[len("(bootloader) ") :]
            if line.startswith(name + ":"):
                value = line[len(name) + 1 :].strip()
                return value or None
        return None

    def getvar_all(self) -> dict[str, str]:
        res = self.tool.run("getvar", "all", timeout=30)
        return parse_getvar_all(res.stderr + "\n" + res.stdout)

    def mode(self) -> str:
        return "fastbootd" if self.getvar("is-userspace") == "yes" else "bootloader"

    def has_slot(self, name: str) -> bool:
        return self.getvar(f"has-slot:{check_name(name)}") == "yes"

    def partition_size(self, name: str) -> int | None:
        value = self.getvar(f"partition-size:{check_name(name)}")
        try:
            return int(value, 16) if value else None
        except ValueError:
            return None

    def is_logical(self, name: str) -> bool | None:
        value = self.getvar(f"is-logical:{check_name(name)}")
        return None if value is None else value == "yes"

    # --- writing ------------------------------------------------------

    def _check(self, args: tuple[str, ...], timeout: float) -> str:
        res = self.tool.run(*args, timeout=timeout)
        out = (res.stderr + "\n" + res.stdout).strip()
        if res.returncode != 0 or "FAILED" in out:
            raise FastbootError(f"fastboot {' '.join(args)} failed: {out.splitlines()[-1] if out else res.returncode}")
        return out

    def flash(self, partition: str, image: Path, timeout: float = 1800, flags: tuple[str, ...] = ()) -> str:
        check_name(partition)
        if self.profile.is_never_flash(partition):
            raise RefusedPartition(f"{partition} is on the never-flash list and will not be written")
        image = Path(image)
        if not image.is_file():
            raise FastbootError(f"image not found: {image}")
        if flags:
            if set(flags) - {"--disable-verity", "--disable-verification"}:
                raise FastbootError(f"unsupported flash flags {flags}")
            if not partition.startswith("vbmeta"):
                raise FastbootError("verity flags only apply to vbmeta")
        return self._check((*flags, "flash", partition, str(image)), timeout)

    def set_active(self, slot: str) -> None:
        if slot not in ("a", "b"):
            raise FastbootError(f"invalid slot {slot!r}")
        self._check(("set_active", slot), 30)

    def wipe_data(self) -> None:
        """Factory reset: erases userdata and metadata."""
        self._check(("-w",), 300)

    def cancel_snapshot_update(self) -> None:
        self._check(("snapshot-update", "cancel"), 60)

    def reboot(self, target: str = "") -> None:
        if target not in ("", "bootloader", "fastboot", "recovery"):
            raise FastbootError(f"invalid reboot target {target!r}")
        args = ("reboot", target) if target else ("reboot",)
        res = self.tool.run(*args, timeout=30)
        if res.returncode != 0:
            raise FastbootError(f"fastboot {' '.join(args)} failed: {res.stderr.strip()}")


def fastboot_serials(fastboot: Tool) -> list[str]:
    res = fastboot.run("devices", timeout=15)
    return parse_fastboot_devices(res.stdout + "\n" + res.stderr)


def wait_for_fastboot(
    fastboot: Tool,
    serial: str | None,
    userspace: bool,
    timeout: float = 120,
    cancel=None,
) -> str:
    """Wait until the phone shows up in bootloader (userspace=False) or fastbootd."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise FastbootError("cancelled while waiting for the phone")
        serials = fastboot_serials(fastboot)
        candidates = [s for s in serials if s == serial] if serial else serials
        if serial and not candidates and len(serials) == 1:
            candidates = serials  # some phones report a different serial in fastboot
        if len(candidates) == 1:
            fb = fastboot.bind(candidates[0])
            res = fb.run("getvar", "is-userspace", timeout=15)
            is_user = "is-userspace: yes" in (res.stderr + res.stdout)
            if is_user == userspace:
                return candidates[0]
        time.sleep(poll_interval())
    where = "fastbootd" if userspace else "the bootloader"
    raise FastbootError(f"The phone did not appear in {where} within {int(timeout)} seconds.")


def wait_for_adb(adb: Tool, serial: str, timeout: float = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = adb.run("devices", timeout=15)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == serial and parts[1] in ("device", "recovery"):
                return True
        time.sleep(poll_interval())
    return False
