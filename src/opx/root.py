"""Phase 5: root assistant.

Patching is done by the root manager app on the phone (Magisk, KernelSU or
APatch), which is how those projects recommend it. OPxFlasher handles the
error-prone parts around it:

  1. take the stock image (init_boot or boot) that matches the build the
     phone is running *right now* - from a full OTA (fingerprint must match)
     or from a backup made on the same build
  2. push it to /sdcard/Download for the app to patch
  3. find the patched file, pull it back and sanity-check it
  4. flash it to the current slot only, behind the same gates as a flash
  5. unroot = flash the stock image back the same way
"""

from __future__ import annotations

import json
import re
import shutil
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path

from opx.backup import manifest_file
from opx.flash import FlashOptions, FlashPlan, Step, check_backup, sha256_file
from opx.plan import DeviceState
from opx.rom import payload as pl
from opx.rom.ota import RomInfo
from opx.tools import Tool

DOWNLOAD = "/sdcard/Download"
MANAGERS = {
    "magisk": ("Magisk", "init_boot"),
    "kernelsu": ("KernelSU", "init_boot"),
    "apatch": ("APatch", "boot"),
}
ROOT_PARTITIONS = {"init_boot", "boot"}
SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}$")


class RootError(RuntimeError):
    pass


def boot_header_version(data: bytes) -> int:
    """Header version of an Android boot / init_boot image (refuses anything else)."""
    if data[:8] == b"VNDRBOOT":
        raise RootError("This is a vendor_boot image, not boot/init_boot.")
    if data[:8] != b"ANDROID!":
        raise RootError("Not an Android boot image (missing ANDROID! header).")
    if len(data) < 44:
        raise RootError("Boot image header is truncated.")
    return struct.unpack_from("<I", data, 40)[0]


@dataclass
class RootSession:
    manager: str
    partition: str
    device: dict[str, str]
    stock_path: str
    stock_sha: str
    source: str
    remote_stock: str = ""
    before: list[str] = field(default_factory=list)
    patched_remote: str = ""
    patched_path: str = ""
    patched_sha: str = ""

    def save(self, workdir: Path) -> Path:
        path = Path(workdir) / "root-session.json"
        path.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        return path

    @classmethod
    def load(cls, workdir: Path) -> RootSession:
        return cls(**json.loads((Path(workdir) / "root-session.json").read_text("utf-8")))


# --- stock image ----------------------------------------------------------------


def stock_from_rom(rom: RomInfo, partition: str, facts: dict[str, str], workdir: Path) -> Path:
    if partition not in ROOT_PARTITIONS:
        raise RootError(f"{partition} is not a root partition")
    if not rom.fingerprint:
        raise RootError("The ROM doesn't state its build. Use a full OTA zip (it has the build metadata).")
    current = facts.get("fingerprint", "")
    if rom.fingerprint != current:
        raise RootError(
            "The ROM is not the build this phone is running, and patching another build's "
            f"{partition} causes a bootloop.\n  ROM:   {rom.fingerprint}\n  Phone: {current or 'unknown'}"
        )
    img = rom.image(partition)
    if img is None:
        raise RootError(f"The ROM has no {partition}.img")
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / f"stock_{partition}.img"
    if img.file is not None:
        shutil.copyfile(img.file, target)
    else:
        pl.extract(rom.payload, partition, target)
    boot_header_version(target.read_bytes()[:64])
    return target


def stock_from_backup(folder: Path, partition: str, facts: dict[str, str], workdir: Path) -> Path:
    if partition not in ROOT_PARTITIONS:
        raise RootError(f"{partition} is not a root partition")
    manifest = json.loads((Path(folder) / "manifest.json").read_text("utf-8"))
    dev = manifest.get("device", {})
    for key in ("serial", "model"):
        if dev.get(key) and facts.get(key) and dev[key] != facts[key]:
            raise RootError(f"The backup is from a different phone ({key} {dev[key]}).")
    if not dev.get("build") or dev.get("build") != facts.get("build"):
        raise RootError(
            f"The backup was made on build {dev.get('build') or '?'}, the phone runs {facts.get('build') or '?'}. "
            "Use a backup from the current build or a matching full OTA."
        )
    slot = facts.get("slot_suffix", "")
    parts = {p["name"]: p for p in manifest.get("partitions", []) if p.get("status") != "failed"}
    entry = parts.get(partition + slot) or parts.get(partition)
    if entry is None:
        raise RootError(f"The backup has no {partition}{slot}. Make a Full backup first.")
    src = manifest_file(folder, entry["file"])
    if sha256_file(src) != entry["sha256"]:
        raise RootError(f"{src.name} in the backup doesn't match its recorded hash.")
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / f"stock_{partition}.img"
    shutil.copyfile(src, target)
    boot_header_version(target.read_bytes()[:64])
    return target


def new_session(manager: str, device: DeviceState, stock: Path, source: str) -> RootSession:
    if manager not in MANAGERS:
        raise RootError(f"Unknown root manager {manager!r}; choose {', '.join(MANAGERS)}")
    return RootSession(
        manager=manager,
        partition=MANAGERS[manager][1],
        device=dict(device.facts),
        stock_path=str(stock),
        stock_sha=sha256_file(stock),
        source=source,
    )


# --- phone side ---------------------------------------------------------------


def list_download(adb: Tool) -> list[str]:
    """Files in /sdcard/Download, newest first."""
    res = adb.run("shell", f"ls -t {DOWNLOAD}", timeout=20)
    if not res.ok:
        raise RootError(f"Couldn't list {DOWNLOAD} on the phone: {res.stderr.strip()}")
    return [n.strip() for n in res.stdout.splitlines() if SAFE_FILE.match(n.strip())]


def push_stock(adb: Tool, session: RootSession) -> str:
    name = f"opx_stock_{session.partition}.img"
    session.before = list_download(adb)
    remote = f"{DOWNLOAD}/{name}"
    res = adb.run("push", session.stock_path, remote, timeout=300)
    if not res.ok:
        raise RootError(f"adb push failed: {res.stderr.strip() or res.stdout.strip()}")
    session.remote_stock = remote
    return remote


def find_patched(adb: Tool, session: RootSession) -> str | None:
    for name in list_download(adb):
        low = name.lower()
        if low.endswith(".img") and "patched" in low and name not in session.before:
            return f"{DOWNLOAD}/{name}"
    return None


def verify_patched(stock: Path, patched: Path) -> list[str]:
    problems = []
    stock_head, patched_head = stock.read_bytes()[:64], patched.read_bytes()[:64]
    try:
        stock_ver = boot_header_version(stock_head)
        patched_ver = boot_header_version(patched_head)
        if stock_ver != patched_ver:
            problems.append(f"Header version changed ({stock_ver} -> {patched_ver}); this isn't a patch of the stock image.")
    except RootError as e:
        problems.append(f"Patched file: {e}")
    if sha256_file(stock) == sha256_file(patched):
        problems.append("The patched file is identical to the stock image; nothing was patched.")
    if patched.stat().st_size < 4096:
        problems.append("The patched file is suspiciously small.")
    return problems


def pull_patched(adb: Tool, session: RootSession, workdir: Path) -> list[str]:
    remote = find_patched(adb, session)
    if remote is None:
        raise RootError(
            f"No new patched image in {DOWNLOAD} yet. In {MANAGERS[session.manager][0]}, patch "
            f"{Path(session.remote_stock).name} and try again."
        )
    local = Path(workdir) / f"patched_{session.partition}.img"
    res = adb.run("pull", remote, str(local), timeout=300)
    if not res.ok or not local.exists():
        raise RootError(f"adb pull failed: {res.stderr.strip() or res.stdout.strip()}")
    session.patched_remote = remote
    session.patched_path = str(local)
    session.patched_sha = sha256_file(local)
    return verify_patched(Path(session.stock_path), local)


# --- flashing -----------------------------------------------------------------


def prepare_flash(
    device: DeviceState,
    backup_folder: Path | None,
    session: RootSession,
    purpose: str,
    reboot_after: bool = True,
) -> FlashPlan:
    """purpose: "root" (flash the patched image) or "unroot" (flash stock back)."""
    problems = check_backup(backup_folder, device.facts)
    blocked = device.profile.blocked_actions(device.props)
    if "bootloader" in blocked:
        problems.append("This phone's current firmware is known to break the bootloader's fastboot mode. Flashing is disabled.")
    if session.partition not in ROOT_PARTITIONS:
        problems.append(f"{session.partition} is not a root partition.")
    if device.facts.get("fingerprint") != session.device.get("fingerprint"):
        problems.append("The phone's build changed since the stock image was prepared. Start again with the new build.")
    if purpose == "root":
        if not session.patched_path:
            problems.append("No patched image yet.")
        else:
            problems += verify_patched(Path(session.stock_path), Path(session.patched_path))
        image, sha, source = session.patched_path, session.patched_sha, "patched"
    elif purpose == "unroot":
        image, sha, source = session.stock_path, session.stock_sha, "stock"
    else:
        raise ValueError(purpose)
    steps = [
        Step("to-bootloader"),
        Step("preflight"),
        Step("flash-current", session.partition, source=source, image=image, sha256=sha),
    ]
    if reboot_after:
        steps.append(Step("reboot"))
    return FlashPlan(
        rom_path="",
        device=dict(device.facts),
        options=FlashOptions(reboot_after=reboot_after),
        steps=steps,
        problems=problems,
        blocked_actions=sorted(blocked),
        backup=str(backup_folder) if backup_folder else None,
    )
