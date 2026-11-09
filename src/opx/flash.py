"""Phase 3: actually flashing a ROM, behind every safety gate we have.

Gates, all before a single byte is written:
  1. The Phase 2 report must have no blocking checks. The only block that can
     be overridden is the *unvalidated* Qualcomm ARB reader, and only on
     purpose (``overrides={"qcom-arb-unvalidated"}``). A lower ARB, a lower AVB
     index, a wrong device or an incremental OTA can never be overridden.
  2. A verified Critical or Full backup of this phone must exist.
  3. The phone's current firmware must not be known to break the reboot we
     need (e.g. ColorOS 17 CN: no fastbootd, no bootloader).
  4. Never-flash partitions are refused again at write time (opx.fastboot).

While flashing: images are extracted and hash-checked first, every step is
journalled, the run stops at the first error, and an interrupted flash can
be resumed from the journal.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from opx import __version__
from opx.fastboot import Fastboot, FastbootError, wait_for_fastboot
from opx.plan import BLOCK, DeviceState, Report
from opx.profile import Profile
from opx.rom import payload as pl
from opx.rom.ota import RomInfo, open_rom
from opx.tools import Tool

OVERRIDE_QCOM_UNVALIDATED = "qcom-arb-unvalidated"
AVB_ORDER_LAST = ("vbmeta_system", "vbmeta_vendor", "vbmeta")

Event = dict
EventFn = Callable[[Event], None]


class FlashError(RuntimeError):
    pass


class Cancelled(FlashError):
    pass


# --- options and gates --------------------------------------------------------


@dataclass
class FlashOptions:
    include_optional: list[str] = field(default_factory=list)
    wipe_data: bool = False
    reboot_after: bool = True
    allow_fastbootd: bool = False
    overrides: list[str] = field(default_factory=list)


def override_key(check) -> str | None:
    if check.status == BLOCK and check.name.startswith("Qualcomm ARB") and "not yet validated" in check.detail:
        return OVERRIDE_QCOM_UNVALIDATED
    return None


def hard_blocks(report: Report, overrides: list[str]) -> list:
    return [c for c in report.checks if c.status == BLOCK and (override_key(c) is None or override_key(c) not in overrides)]


def overridable_blocks(report: Report) -> list:
    return [c for c in report.checks if override_key(c) is not None]


def check_backup(folder: Path | None, facts: dict[str, str]) -> list[str]:
    """A verified Critical or Full backup of this phone is required before flashing."""
    if folder is None:
        return ["Choose a backup of this phone (Critical or Full) before flashing."]
    manifest_path = Path(folder) / "manifest.json"
    if not manifest_path.exists():
        return [f"{folder} is not an OPxFlasher backup (no manifest.json)."]
    manifest = json.loads(manifest_path.read_text("utf-8"))
    problems = []
    if not manifest.get("finished"):
        problems.append("The backup did not finish.")
    if manifest.get("mode") not in ("critical", "full"):
        problems.append("Use a Critical or Full backup; Network-only isn't enough to recover from a bad flash.")
    dev = manifest.get("device", {})
    for key, label in (("serial", "serial"), ("model", "model")):
        if dev.get(key) and facts.get(key) and dev[key] != facts[key]:
            problems.append(f"The backup is from a different phone ({label} {dev[key]}, this phone {facts[key]}).")
    parts = manifest.get("partitions", [])
    failed = [p["name"] for p in parts if p.get("status") == "failed"]
    if failed:
        problems.append("Some backup images failed: " + ", ".join(failed))
    if not any(p["name"].startswith("oplusstanvbk") for p in parts):
        problems.append("The backup has no oplusstanvbk (network/region file).")
    return problems


# --- steps ----------------------------------------------------------------------


@dataclass
class Step:
    kind: str  # to-bootloader, preflight, flash, to-fastbootd, snapshot, flash-logical, flash-super, back-to-bootloader, wipe, reboot
    partition: str = ""
    status: str = "pending"  # pending, done, failed
    detail: str = ""

    @property
    def label(self) -> str:
        return {
            "to-bootloader": "Reboot to bootloader",
            "preflight": "Check bootloader (unlocked, slot)",
            "flash": f"Flash {self.partition} (both slots)",
            "to-fastbootd": "Reboot to fastbootd",
            "snapshot": "Cancel any pending OTA snapshot",
            "flash-logical": f"Flash {self.partition} (logical, in fastbootd)",
            "flash-super": "Flash super.img",
            "back-to-bootloader": "Reboot back to bootloader",
            "wipe": "Wipe data (factory reset)",
            "reboot": "Reboot to Android",
        }[self.kind]

    @property
    def mode(self) -> str | None:
        """The mode the phone must be in before this step runs."""
        if self.kind in ("preflight", "flash", "flash-super", "to-fastbootd", "wipe", "reboot"):
            return "bootloader"
        if self.kind in ("snapshot", "flash-logical", "back-to-bootloader"):
            return "fastbootd"
        return None


@dataclass
class FlashPlan:
    rom_path: str
    device: dict[str, str]
    options: FlashOptions
    steps: list[Step]
    problems: list[str]
    blocked_actions: list[str]
    backup: str | None

    @property
    def ok(self) -> bool:
        return not self.problems


def prepare(
    rom: RomInfo,
    report: Report,
    device: DeviceState,
    options: FlashOptions,
    backup_folder: Path | None,
) -> FlashPlan:
    problems: list[str] = []
    for c in hard_blocks(report, options.overrides):
        problems.append(f"{c.name}: {c.detail}")
    problems += check_backup(backup_folder, device.facts)

    blocked = device.profile.blocked_actions(device.props)
    if "bootloader" in blocked:
        problems.append("This phone's current firmware is known to break the bootloader's fastboot mode. Flashing is disabled.")

    items = report.items
    optional = {i.partition for i in items if i.action == "conditional"}
    unknown_optional = sorted(set(options.include_optional) - optional)
    if unknown_optional:
        problems.append("Not optional partitions in this ROM: " + ", ".join(unknown_optional))

    chosen = set(options.include_optional)
    physical = [i.partition for i in items if not i.logical and (i.action == "flash" or (i.action == "conditional" and i.partition in chosen))]
    logical = [i.partition for i in items if i.logical and (i.action == "super" or (i.action == "conditional" and i.partition in chosen))]
    has_super_image = "super" in physical
    if has_super_image:
        physical.remove("super")
    physical.sort(key=lambda n: (AVB_ORDER_LAST.index(n) + 1) if n in AVB_ORDER_LAST else 0)

    steps = [Step("to-bootloader"), Step("preflight")]
    steps += [Step("flash", name) for name in physical]
    if has_super_image:
        steps.append(Step("flash-super", "super", detail="super.img replaces every logical partition, including my_region"))
    elif logical:
        if "fastbootd" in blocked:
            problems.append("Logical partitions need fastbootd, which is known to be broken on this firmware.")
        elif not options.allow_fastbootd:
            problems.append(
                "This ROM has logical partitions (" + ", ".join(logical[:4]) + ("..." if len(logical) > 4 else "")
                + ") and no super.img. Allow fastbootd to flash them, or use a ROM folder that includes super.img."
            )
        else:
            steps += [Step("to-fastbootd"), Step("snapshot")]
            steps += [Step("flash-logical", name) for name in logical]
            steps.append(Step("back-to-bootloader"))
    if options.wipe_data:
        steps.append(Step("wipe"))
    if options.reboot_after:
        steps.append(Step("reboot"))

    return FlashPlan(
        rom_path=str(rom.path),
        device=dict(device.facts),
        options=options,
        steps=steps,
        problems=problems,
        blocked_actions=sorted(blocked),
        backup=str(backup_folder) if backup_folder else None,
    )


# --- journal ------------------------------------------------------------------


def save_journal(plan: FlashPlan, path: Path) -> None:
    data = {
        "tool": "OPxFlasher",
        "tool_version": __version__,
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **asdict(plan),
    }
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(path)


def load_journal(path: Path) -> FlashPlan:
    data = json.loads(Path(path).read_text("utf-8"))
    return FlashPlan(
        rom_path=data["rom_path"],
        device=data["device"],
        options=FlashOptions(**data["options"]),
        steps=[Step(**s) for s in data["steps"]],
        problems=data["problems"],
        blocked_actions=data["blocked_actions"],
        backup=data.get("backup"),
    )


# --- running ------------------------------------------------------------------


def _image_names(plan: FlashPlan) -> list[str]:
    return [s.partition for s in plan.steps if s.kind in ("flash", "flash-logical", "flash-super")]


def extract_images(rom: RomInfo, names: list[str], workdir: Path, emit: EventFn, cancel: threading.Event) -> dict[str, Path]:
    workdir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    for i, name in enumerate(names, 1):
        if cancel.is_set():
            raise Cancelled("cancelled before flashing started; nothing was written")
        img = rom.image(name)
        if img is None:
            raise FlashError(f"{name} is not in the ROM")
        emit({"type": "extract", "name": name, "index": i, "total": len(names)})
        if img.file is not None:
            files[name] = img.file
        else:
            target = workdir / f"{name}.img"
            if not (target.exists() and target.stat().st_size == img.size):
                pl.extract(rom.payload, name, target)
            files[name] = target
    return files


class Runner:
    def __init__(
        self,
        plan: FlashPlan,
        rom: RomInfo,
        profile: Profile,
        adb: Tool,
        fastboot: Tool,
        journal: Path,
        workdir: Path,
        on_event: EventFn | None = None,
        cancel: threading.Event | None = None,
        wait_timeout: float = 120,
    ):
        self.plan = plan
        self.rom = rom
        self.profile = profile
        self.adb = adb
        self.fastboot_tool = fastboot
        self.journal = Path(journal)
        self.workdir = Path(workdir)
        self.emit = on_event or (lambda e: None)
        self.cancel = cancel or threading.Event()
        self.wait_timeout = wait_timeout
        self.serial = plan.device.get("serial") or None
        self.fb: Fastboot | None = None

    # --- mode handling ------------------------------------------------

    def _adb_in_android(self) -> bool:
        res = self.adb.run("devices", timeout=15)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("device", "recovery") and (not self.serial or parts[0] == self.serial):
                return True
        return False

    def _connect(self, userspace: bool) -> Fastboot:
        serial = wait_for_fastboot(self.fastboot_tool, self.serial, userspace, self.wait_timeout, self.cancel)
        self.serial = self.serial or serial
        self.fb = Fastboot(self.fastboot_tool.bind(serial), self.profile)
        return self.fb

    def _to_bootloader(self) -> None:
        if "bootloader" in self.plan.blocked_actions:
            raise FlashError("Refusing to reboot to the bootloader: this firmware is known to break it.")
        if self._adb_in_android():
            self.emit({"type": "log", "message": "Rebooting the phone to the bootloader..."})
            adb = self.adb.bind(self.serial) if self.serial else self.adb
            adb.run("reboot", "bootloader", timeout=30)
            self._connect(userspace=False)
            return
        try:
            fb = self._connect_now()
        except FastbootError:
            fb = self._connect(userspace=False)
        if fb.mode() == "fastbootd":
            fb.reboot("bootloader")
            self._connect(userspace=False)

    def _connect_now(self) -> Fastboot:
        return Fastboot(self.fastboot_tool.bind(self._find_now()), self.profile)

    def _find_now(self) -> str:
        from opx.fastboot import fastboot_serials

        serials = fastboot_serials(self.fastboot_tool)
        if self.serial in serials:
            return self.serial
        if len(serials) == 1:
            return serials[0]
        raise FastbootError("phone not found in fastboot")

    def _ensure(self, mode: str) -> Fastboot:
        """Before each step: the phone must already be in the expected mode."""
        if self.fb is None:
            try:
                self.fb = self._connect_now()
            except FastbootError:
                if mode == "bootloader" and self._adb_in_android():
                    self._to_bootloader()  # resuming after the phone booted back to Android
                else:
                    raise
        fb = self.fb
        actual = fb.mode()
        if actual != mode:
            raise FlashError(f"Expected the phone in {mode}, but it is in {actual}. Stopping.")
        return fb

    # --- steps --------------------------------------------------------

    def _flash_physical(self, fb: Fastboot, name: str, image: Path) -> str:
        targets = [f"{name}_a", f"{name}_b"] if fb.has_slot(name) else [name]
        size = image.stat().st_size
        for target in targets:
            part_size = fb.partition_size(target)
            if part_size is None:
                raise FlashError(f"{target} does not exist on this phone")
            if size > part_size:
                raise FlashError(f"{name}.img ({size} bytes) is larger than {target} ({part_size} bytes)")
        for target in targets:
            fb.flash(target, image)
        return " + ".join(targets)

    def _run_step(self, step: Step, files: dict[str, Path]) -> str:
        if step.kind == "to-bootloader":
            self._to_bootloader()
            return "in bootloader"
        if step.kind == "back-to-bootloader":
            fb = self._ensure("fastbootd")
            fb.reboot("bootloader")
            self._connect(userspace=False)
            return "in bootloader"
        if step.kind == "to-fastbootd":
            if "fastbootd" in self.plan.blocked_actions:
                raise FlashError("Refusing to reboot to fastbootd: this firmware is known to break it.")
            fb = self._ensure("bootloader")
            fb.reboot("fastboot")
            self._connect(userspace=True)
            return "in fastbootd"

        fb = self._ensure(step.mode)
        if step.kind == "preflight":
            if fb.getvar("unlocked") != "yes":
                raise FlashError("The bootloader is locked. Flashing is not possible.")
            return f"unlocked, current slot {fb.getvar('current-slot') or '?'}"
        if step.kind == "flash":
            return "wrote " + self._flash_physical(fb, step.partition, files[step.partition])
        if step.kind == "flash-super":
            size = files["super"].stat().st_size
            super_size = fb.partition_size("super")
            if super_size is None or size > super_size:
                raise FlashError(f"super.img ({size} bytes) does not fit super ({super_size})")
            fb.flash("super", files["super"])
            return "wrote super"
        if step.kind == "snapshot":
            status = fb.getvar("snapshot-update-status")
            if status and status != "none":
                fb.cancel_snapshot_update()
                return f"cancelled ({status})"
            return "none pending"
        if step.kind == "flash-logical":
            fb.flash(step.partition, files[step.partition])
            return f"wrote {step.partition} (current slot)"
        if step.kind == "wipe":
            fb.wipe_data()
            return "userdata and metadata erased"
        if step.kind == "reboot":
            fb.reboot()
            return "rebooting to Android"
        raise FlashError(f"unknown step {step.kind}")

    def run(self) -> FlashPlan:
        plan = self.plan
        if not plan.ok:
            raise FlashError("Flashing is blocked:\n- " + "\n- ".join(plan.problems))
        files = extract_images(self.rom, _image_names(plan), self.workdir, self.emit, self.cancel)
        save_journal(plan, self.journal)
        pending = [s for s in plan.steps if s.status != "done"]
        total = len(plan.steps)
        for step in pending:
            index = plan.steps.index(step) + 1
            if self.cancel.is_set():
                save_journal(plan, self.journal)
                raise Cancelled(f"Stopped before step {index} ({step.label}). Resume with the journal: {self.journal}")
            self.emit({"type": "step_start", "index": index, "total": total, "label": step.label})
            try:
                step.detail = self._run_step(step, files)
                step.status = "done"
            except (FastbootError, FlashError, OSError) as exc:
                step.status = "failed"
                step.detail = str(exc)
                save_journal(plan, self.journal)
                self.emit({"type": "step_done", "index": index, "label": step.label, "status": "failed", "detail": step.detail})
                raise FlashError(
                    f"Step {index} ({step.label}) failed: {exc}\n"
                    "Do not reboot or unplug the phone. Fix the cause, then resume from the journal: "
                    f"{self.journal}"
                ) from exc
            save_journal(plan, self.journal)
            self.emit({"type": "step_done", "index": index, "label": step.label, "status": "done", "detail": step.detail})
        self.emit({"type": "finished", "journal": str(self.journal)})
        return plan


def resume_runner(journal: Path, profile: Profile, adb: Tool, fastboot: Tool, **kwargs) -> Runner:
    plan = load_journal(journal)
    for step in plan.steps:
        if step.status == "failed":
            step.status = "pending"
    rom = open_rom(plan.rom_path)
    return Runner(plan, rom, profile, adb, fastboot, journal, Path(journal).parent / "images", **kwargs)
