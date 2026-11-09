"""Finding connected phones and reading basic, read-only information."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from opx.tools import Tool, ToolNotFound

ADB_STATES = {"device", "recovery", "sideload", "unauthorized", "offline", "rescue", "bootloader"}


@dataclass
class Device:
    serial: str
    transport: str  # "adb" or "fastboot"
    state: str  # adb: device/recovery/unauthorized/...; fastboot: bootloader/fastbootd
    model: str = ""
    product: str = ""
    codename: str = ""

    @property
    def label(self) -> str:
        name = self.model or self.product or "Unknown device"
        return f"{name} ({self.serial}) - {self.mode_text}"

    @property
    def mode_text(self) -> str:
        return {
            "device": "Android (ADB)",
            "recovery": "Recovery (ADB)",
            "sideload": "Sideload",
            "unauthorized": "ADB not authorised - accept the prompt on the phone",
            "offline": "Offline",
            "bootloader": "Bootloader (fastboot)",
            "fastbootd": "fastbootd",
        }.get(self.state, self.state)

    @property
    def can_backup(self) -> bool:
        return self.transport == "adb" and self.state in ("device", "recovery")


def parse_adb_devices(text: str) -> list[Device]:
    devices = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[1] not in ADB_STATES:
            continue
        extras = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(
            Device(
                serial=parts[0],
                transport="adb",
                state=parts[1],
                model=extras.get("model", "").replace("_", " "),
                product=extras.get("product", ""),
                codename=extras.get("device", ""),
            )
        )
    return devices


def parse_fastboot_devices(text: str) -> list[str]:
    serials = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ("fastboot", "fastbootd"):
            serials.append(parts[0])
    return serials


def parse_getvar_all(text: str) -> dict[str, str]:
    """Parse ``fastboot getvar all`` output ("(bootloader) key:value")."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\(bootloader\)\s+(.+?):\s*(.*)$", line.strip())
        if m:
            key, value = m.group(1).strip(), m.group(2).strip()
            # keep the first value; later partition-* lines are numerous
            out.setdefault(key, value)
    return out


@dataclass
class Scan:
    devices: list[Device] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def scan(adb: Tool | None = None, fastboot: Tool | None = None) -> Scan:
    result = Scan()
    try:
        adb = adb or Tool("adb")
        res = adb.run("devices", "-l", timeout=15)
        result.devices += parse_adb_devices(res.stdout)
    except ToolNotFound as e:
        result.errors.append(str(e))

    try:
        fastboot = fastboot or Tool("fastboot")
        res = fastboot.run("devices", timeout=15)
        for serial in parse_fastboot_devices(res.stdout + "\n" + res.stderr):
            fb = fastboot.bind(serial)
            info = parse_getvar_all(_getvar_all(fb))
            state = "fastbootd" if info.get("is-userspace") == "yes" else "bootloader"
            result.devices.append(
                Device(serial=serial, transport="fastboot", state=state, product=info.get("product", ""))
            )
    except ToolNotFound as e:
        result.errors.append(str(e))
    return result


def _getvar_all(fb: Tool) -> str:
    res = fb.run("getvar", "all", timeout=20)
    # fastboot prints getvar output on stderr
    return res.stderr + "\n" + res.stdout


def fastboot_summary(fastboot: Tool) -> dict[str, str]:
    """Read-only bootloader facts used for pre-flight checks."""
    info = parse_getvar_all(_getvar_all(fastboot))
    keys = [
        "product",
        "unlocked",
        "secure",
        "current-slot",
        "slot-count",
        "is-userspace",
        "version-bootloader",
        "max-download-size",
    ]
    return {k: info[k] for k in keys if k in info}
