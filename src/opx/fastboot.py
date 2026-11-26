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


class LostPhone(FastbootError):
    """The USB link broke mid-command (the phone dropped off or restarted): not the phone refusing."""


class NoAnswer(LostPhone):
    """A question to the phone got no answer at all (as opposed to the phone saying "not found")."""


# fastboot's own words when the USB transfer itself fails, as opposed to the phone answering FAILED (remote: ...)
TRANSPORT_ERRORS = (
    "status read failed", "write to device failed", "read from device failed", "too many links",
    "no such device", "data transfer failure", "protocol fault", "usb_write failed", "usb_read failed",
    "device not found", "couldn't parse", "no link",
)


def is_transport_error(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in TRANSPORT_ERRORS)


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


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

    def query(self, name: str, timeout: float = 12) -> str | None:
        """getvar that tells silence from "no": the value, None when the phone answers that it has no
        such variable, NoAnswer when it doesn't answer at all (USB link down, phone gone or stuck).
        A silent phone is never read as "this partition doesn't exist" or "this isn't fastbootd"."""
        res = self.tool.run("getvar", name, timeout=timeout)
        out = res.stderr + "\n" + res.stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("(bootloader) "):
                line = line[len("(bootloader) ") :]
            if line.startswith(name + ":"):
                value = line[len(name) + 1 :].strip()
                if value and not (value.startswith("FAILED") and is_transport_error(value)):
                    return value
        low = out.lower()
        if res.returncode == 124 or "waiting for" in low or (is_transport_error(out) and "remote:" not in low):
            raise NoAnswer(f"the phone didn't answer 'getvar {name}' ({_last_line(out) or 'no reply'})")
        return None

    def getvar_all(self) -> dict[str, str]:
        res = self.tool.run("getvar", "all", timeout=30)
        return parse_getvar_all(res.stderr + "\n" + res.stdout)

    def mode(self) -> str:
        return "fastbootd" if self.query("is-userspace") == "yes" else "bootloader"

    def has_slot(self, name: str) -> bool:
        return self.query(f"has-slot:{check_name(name)}") == "yes"

    def partition_size(self, name: str) -> int | None:
        value = self.query(f"partition-size:{check_name(name)}")
        try:
            return int(value, 16) if value else None
        except ValueError:
            return None

    def is_logical(self, name: str) -> bool | None:
        value = self.query(f"is-logical:{check_name(name)}")
        return None if value is None else value == "yes"

    # --- writing ------------------------------------------------------

    def _check(self, args: tuple[str, ...], timeout: float) -> str:
        res = self.tool.run(*args, timeout=timeout)
        out = (res.stderr + "\n" + res.stdout).strip()
        if res.returncode != 0 or "FAILED" in out:
            # the phone's own reason ("FAILED (remote: '...')") is usually not the last line
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            why = [ln for ln in lines if "FAILED" in ln or "remote:" in ln or "error" in ln.lower()]
            text = " / ".join(dict.fromkeys(why or lines[-1:])) or str(res.returncode)
            if res.returncode == 124 or (is_transport_error(out) and "remote:" not in out.lower()):
                raise LostPhone(f"fastboot {' '.join(args)} failed: {text}")
            raise FastbootError(f"fastboot {' '.join(args)} failed: {text}")
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

    def delete_logical(self, name: str) -> bool:
        """fastbootd: remove a logical partition from super (e.g. the other slot's copy). False if it
        wasn't there or couldn't be removed; never raises for that."""
        res = self.tool.run("delete-logical-partition", check_name(name), timeout=60)
        out = (res.stderr + res.stdout)
        return res.returncode == 0 and "FAILED" not in out

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
        res = self.tool.run(*args, timeout=90 if target in ("fastboot", "bootloader") else 30)
        if res.returncode == 124 and target in ("fastboot", "bootloader"):
            # fastboot itself waits for the phone to come back in the new mode, and on Windows it can
            # hang there (the phone shows up under another USB id). The reboot was sent: whoever called
            # this waits for the phone in the new mode and fails if it never appears.
            return
        if res.returncode != 0:
            raise FastbootError(f"fastboot {' '.join(args)} failed: {res.stderr.strip()}")


def fastboot_serials(fastboot: Tool) -> list[str]:
    res = fastboot.run("devices", timeout=15)
    return parse_fastboot_devices(res.stdout + "\n" + res.stderr)


REPLUG_HINT = (
    "Still waiting for the phone. If its screen already shows {where}, unplug the USB cable and plug it back in: "
    "Windows sometimes misses the switch to {where} until the phone is plugged in again."
)


def wait_for_fastboot(
    fastboot: Tool,
    serial: str | None,
    userspace: bool | None,
    timeout: float = 120,
    cancel=None,
    on_slow=None,
    slow_after: float = 20,
) -> str:
    """Wait until the phone shows up in bootloader (userspace=False), fastbootd (True) or either (None).
    `on_slow(text)` is called once if it takes longer than `slow_after` seconds (the replug hint). A phone
    that's listed but doesn't answer is still being waited for: silence never counts as the bootloader."""
    start = time.monotonic()
    deadline = start + timeout
    hinted = False
    where = "fastboot mode" if userspace is None else "fastbootd" if userspace else "the bootloader"
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
            out = res.stderr + res.stdout
            low = out.lower()
            answered = res.returncode != 124 and "waiting for" not in low and not (
                is_transport_error(out) and "remote:" not in low)
            if answered:
                is_user = "is-userspace: yes" in out
                if userspace is None or is_user == userspace:
                    return candidates[0]
        if on_slow is not None and not hinted and time.monotonic() - start > slow_after:
            hinted = True
            on_slow(REPLUG_HINT.format(where=where))
        time.sleep(poll_interval())
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
