"""Phase 3: actually flashing a ROM, behind every safety gate we have.

Gates, all before a single byte is written:
  1. The Phase 2 report must have no blocking checks. The only block that can
     be overridden is the *unvalidated* Qualcomm ARB reader, and only on
     purpose (``overrides={"qcom-arb-unvalidated"}``). A lower ARB, a lower AVB
     index, a wrong device or an incremental OTA can never be overridden.
  2. A verified Critical or Full backup of this phone, unless the user typed NO BACKUP (then a
     backup is only recommended; one that IS chosen must still be this phone's).
  3. The phone's current firmware must not be known to break the reboot we
     need (e.g. ColorOS 17 CN: no fastbootd, no bootloader).
  4. Never-flash partitions are refused again at write time (opx.fastboot).

While flashing: images are extracted and hash-checked first, every step is
journalled, the run stops at the first error, and an interrupted flash can
be resumed from the journal.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from opx import __version__
from opx.backup import manifest_file
from opx.fastboot import Fastboot, FastbootError, LostPhone, fastboot_serials, wait_for_fastboot
from opx.plan import BLOCK, DeviceState, Report
from opx.profile import Profile
from opx.rom.ota import RomInfo, materialize, open_rom
from opx.tools import Tool

OVERRIDE_QCOM_UNVALIDATED = "qcom-arb-unvalidated"
OVERRIDE_VARIANT = "variant-change"  # same phone model, other region: user confirms
OVERRIDE_UNKNOWN_FIRMWARE = "restore-phone-not-booted"  # full ROM restore with the phone not in Android
OVERRIDE_UNKNOWN_BUILD = "installed-build-unknown"  # build unknown (phone or ROM): the user confirms "same or newer"
OVERRIDE_UNKNOWN_ROM = "rom-identity-unknown"  # loose images: the user confirms they're for this phone
# confirmed together in the anti-rollback warning
RISK_OVERRIDES = (OVERRIDE_QCOM_UNVALIDATED, OVERRIDE_UNKNOWN_BUILD, OVERRIDE_UNKNOWN_ROM)
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
    restore_from_backup: list[str] = field(default_factory=list)  # region files written from YOUR backup
    keep_recovery: bool = False  # don't write recovery: a custom recovery (AERA, OrangeFox) stays
    no_backup: bool = False  # the user typed NO BACKUP: a backup is recommended, not required
    network_backup: str = ""  # where restore_from_backup comes from, if not the main backup (Network essentials)


def override_key(check) -> str | None:
    if check.status == BLOCK and check.name.startswith("Qualcomm ARB") and (
            "not yet validated" in check.detail or "can't be read without root" in check.detail):
        return OVERRIDE_QCOM_UNVALIDATED
    if check.status == BLOCK and check.name == "Variant":
        return OVERRIDE_VARIANT
    if check.status == BLOCK and check.name == "Current firmware":
        return OVERRIDE_UNKNOWN_FIRMWARE
    if check.status == BLOCK and check.name in ("Installed build", "ROM age"):
        return OVERRIDE_UNKNOWN_BUILD
    if check.status == BLOCK and check.name == "ROM identity":
        return OVERRIDE_UNKNOWN_ROM
    return None


def hard_blocks(report: Report, overrides: list[str]) -> list:
    return [c for c in report.checks if c.status == BLOCK and (override_key(c) is None or override_key(c) not in overrides)]


def overridable_blocks(report: Report, key: str | None = None) -> list:
    return [c for c in report.checks if override_key(c) is not None and (key is None or override_key(c) == key)]


# shown before the user confirms anti-rollback themselves (GUI dialog, CLI prompt)
ARB_WARNINGS = (
    "Qualcomm phones carry anti-rollback fuses. Firmware (xbl, xbl_config, abl ...) with a LOWER anti-rollback "
    "version than the phone's is refused by the chip itself: the phone no longer starts.",
    "OnePlus 13 / 13T / 15 from ColorOS 16.0.3.501 also burn a fuse when an older build is installed. That damage "
    "is permanent.",
    "A phone bricked this way can't be fixed with fastboot, recovery or OPxFlasher. Sometimes only an authorised "
    "OnePlus / OPPO service centre (official EDL tools, often a board replacement) can repair it, and sometimes "
    "nothing can.",
    "Only flash the same build as the phone, or a newer one, of your phone's own model line. Never go back to an "
    "older build to \"fix\" something.",
    "Make sure the ROM is complete and works on your phone (check it in Verify ROM; read reports from other users).",
)
NO_BACKUP_ROM_WARNING = ("No backup: you chose to flash without one. If the phone doesn't start afterwards, there's "
                         "no backup of this phone to restore it from; the ROM itself can be flashed again.")
NO_BACKUP_WARNING = ("No backup: you chose to flash without one. Only this image is written; if the phone doesn't "
                     "start afterwards, there's no backup of this phone to restore it from.")


def backup_problems(folder: Path | None, device, skip: bool, warnings: list[str]) -> list[str]:
    """check_backup for single-image and root flashes. With `skip` (typed NO BACKUP) a missing or
    unusable backup doesn't block; a backup that IS chosen is still checked, so the wrong phone's
    backup is never silently accepted."""
    if skip and not folder:
        warnings.append(NO_BACKUP_WARNING)
        return []
    return check_backup(folder, device.facts, device.profile)


def check_backup(folder: Path | None, facts: dict[str, str], profile: Profile | None = None) -> list[str]:
    """A verified Critical or Full backup of this phone is required before flashing.

    The serial number says which phone it is. The model changes when the phone is converted to another
    region (PJZ110 -> CPH2653), so a backup from before the conversion still counts when the serials
    match and both models are variants of the phone's profile."""
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
    same_serial = bool(dev.get("serial") and facts.get("serial") and dev["serial"] == facts["serial"])
    if dev.get("serial") and facts.get("serial") and not same_serial:
        problems.append(f"The backup is from a different phone (serial {dev['serial']}, this phone {facts['serial']}).")
    if dev.get("model") and facts.get("model") and dev["model"] != facts["model"]:
        converted = (same_serial and profile is not None
                     and profile.is_variant(dev["model"]) and profile.is_variant(facts["model"]))
        if not converted:
            problems.append(f"The backup is from a different phone (model {dev['model']}, this phone {facts['model']}).")
    parts = manifest.get("partitions", [])
    failed = [p["name"] for p in parts if p.get("status") == "failed"]
    if failed:
        problems.append("Some backup images failed: " + ", ".join(failed))
    if not any(p["name"].startswith("oplusstanvbk") for p in parts):
        problems.append("The backup has no oplusstanvbk (network/region file).")
    return problems


def backup_images(folder: Path | str | None, name: str) -> dict[str, tuple[Path, str, str]]:
    """Backed-up images of one partition: {full_name: (file, sha256, source)}."""
    if not folder:
        return {}
    manifest_path = Path(folder) / "manifest.json"
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text("utf-8"))
    out = {}
    for part in manifest.get("partitions", []):
        if part.get("status") == "failed":
            continue
        if part["name"] in (name, f"{name}_a", f"{name}_b"):
            out[part["name"]] = (manifest_file(folder, part["file"]), part["sha256"], part.get("source", "by-name"))
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def keepable(report: Report | None, names) -> list[str]:
    """Of `names`, the region files this ROM leaves optional (so the phone's own copy can be kept
    or restored from a backup). Ones the ROM's vbmeta checks at boot always come from the ROM."""
    optional = {i.partition for i in report.items if i.action == "conditional"} if report else set()
    return [n for n in names if n in optional]


def region_change(report: Report | None) -> bool:
    return bool(report) and any(c.name == "Variant" and c.status == BLOCK for c in report.checks)


# --- steps ----------------------------------------------------------------------


@dataclass
class Step:
    kind: str  # to-bootloader, preflight, wipe, flash, flash-super, to-fastbootd, snapshot, flash-logical, back-to-bootloader, reboot(-from-fastbootd)
    partition: str = ""
    status: str = "pending"  # pending, done, failed
    detail: str = ""
    source: str = "rom"  # "rom", "backup", or "patched"/"stock" for a single checked image file
    image: str = ""  # explicit image file (root assistant)
    sha256: str = ""  # expected hash of `image`, checked again right before writing
    slot: str = ""  # flash-image: "current", "other" or "both"
    flags: list[str] = field(default_factory=list)  # flash-image: e.g. --disable-verity for vbmeta

    @property
    def label(self) -> str:
        if self.kind == "flash-current":
            return f"Flash {self.source} {self.partition} to the current slot"
        if self.kind == "flash-image":
            where = {"current": "the current slot", "other": "the other slot", "both": "both slots"}[self.slot]
            extra = " with verity disabled" if self.flags else ""
            return f"Flash {Path(self.image).name} to {self.partition} ({where}){extra}"
        if self.source == "backup":
            where = {"flash": "both slots", "flash-any": "both slots",
                     "flash-fw": "both slots, in fastbootd"}.get(self.kind, "logical, in fastbootd")
            return f"Restore {self.partition} from your backup ({where})"
        return {
            "to-bootloader": "Reboot to bootloader",
            "preflight": "Check bootloader (unlocked, slot)",
            "flash": f"Flash {self.partition} (both slots)",
            "flash-fw": f"Flash {self.partition} (both slots, in fastbootd)",
            "flash-any": f"Flash {self.partition} (both slots)",
            "free-super": "Make room in super (remove the other slot's copies)",
            "to-fastbootd": "Reboot to fastbootd",
            "snapshot": "Cancel any pending OTA snapshot",
            "flash-logical": f"Flash {self.partition} (logical, in fastbootd)",
            "flash-super": "Flash super.img",
            "back-to-bootloader": "Reboot back to bootloader",
            "wipe": "Wipe data (factory reset)",
            "reboot": "Reboot to Android",
            "reboot-from-fastbootd": "Reboot to Android",
        }[self.kind]

    @property
    def mode(self) -> str | None:
        """The mode the phone must be in before this step runs (flash-any: either)."""
        if self.kind == "flash-any":
            return None
        if self.kind in ("preflight", "flash", "flash-super", "flash-current", "flash-image", "to-fastbootd", "wipe", "reboot"):
            return "bootloader"
        if self.kind in ("snapshot", "flash-fw", "free-super", "flash-logical", "back-to-bootloader",
                         "reboot-from-fastbootd"):
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
    warnings: list[str] = field(default_factory=list)  # shown, don't block

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
    warnings: list[str] = []
    for c in hard_blocks(report, options.overrides):
        problems.append(f"{c.name}: {c.detail}")
    if not options.no_backup:
        problems += check_backup(backup_folder, device.facts, device.profile)
    elif backup_folder is None:
        warnings.append(NO_BACKUP_ROM_WARNING)
    else:
        # no backup required, but a folder that IS chosen (e.g. Network essentials, for the phone's own
        # network files) must still be this phone's and complete
        problems += [p for p in check_backup(backup_folder, device.facts, device.profile)
                     if not p.startswith("Use a Critical or Full backup")]
        warnings.append("Not a full backup: only your network files can come from it." if _mode(backup_folder)
                        not in ("critical", "full") else "")
        warnings = [w for w in warnings if w]

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
    if options.keep_recovery:
        physical = [n for n in physical if n != "recovery"]

    if region_change(report) and not options.wipe_data:
        problems.append(
            "This ROM is for a different region/variant. Converting needs a data wipe (factory reset); "
            "the phone would not boot with the old data."
        )
    from_rom = {i.partition for i in items if device.profile.is_conditional(i.partition) and i.action != "conditional"}
    for name in dict.fromkeys(options.restore_from_backup):
        if name in from_rom:
            problems.append(f"{name} can't come from your backup with this ROM: its vbmeta checks {name} at boot, "
                            "so an old copy would fail verification. It's written from the ROM.")
    net_folder = Path(options.network_backup) if options.network_backup else backup_folder
    if options.network_backup and options.restore_from_backup:
        # your own network files from their own backup (Network essentials is enough): still this phone's
        problems += [f"Network files backup: {p}" for p in check_backup(net_folder, device.facts, device.profile)
                     if not p.startswith("Use a Critical or Full backup")]
    restore_physical, restore_logical = _restore_targets(
        options, device.profile, net_folder, chosen | from_rom, problems,
        skip=from_rom)
    if region_change(report) and not (restore_physical or restore_logical) and keepable(
            report, device.profile.conditional):
        warnings.append("Region change without your own network files: mobile network may not work afterwards. "
                        "You can restore them later from a backup (Signal / region).")

    steps = _assemble(
        rom_physical=physical,
        rom_logical=[] if has_super_image else logical,
        super_image=has_super_image,
        restore_physical=restore_physical,
        restore_logical=restore_logical,
        options=options,
        blocked=blocked,
        problems=problems,
        fw_in_fastbootd=device.profile.firmware_in_fastbootd,
        bootloader_images=device.profile.bootloader_images,
    )

    return FlashPlan(
        rom_path=str(rom.path),
        device=dict(device.facts),
        options=options,
        steps=steps,
        problems=problems,
        blocked_actions=sorted(blocked),
        backup=str(backup_folder) if backup_folder else None,
        warnings=warnings,
    )


def _mode(folder: Path) -> str:
    try:
        return json.loads((Path(folder) / "manifest.json").read_text("utf-8")).get("mode", "")
    except (OSError, ValueError):
        return ""


def _restore_targets(options, profile, backup_folder, chosen, problems, skip=frozenset()) -> tuple[list[str], list[str]]:
    restore_physical, restore_logical = [], []
    for name in dict.fromkeys(options.restore_from_backup):
        if name in skip:
            continue  # already reported
        if not profile.is_conditional(name):
            problems.append(
                f"{name} can't be restored by OPxFlasher; only region files ({', '.join(profile.conditional)}) can."
            )
            continue
        if name in chosen:
            problems.append(f"{name} is set to come from both the ROM and your backup. Choose one.")
            continue
        images = backup_images(backup_folder, name)
        if not images:
            problems.append(f"Your backup has no {name} image to restore.")
            continue
        if any(src == "mapper" for _, _, src in images.values()):
            restore_logical.append(name)
        else:
            restore_physical.append(name)
    return restore_physical, restore_logical


def _assemble(rom_physical, rom_logical, super_image, restore_physical, restore_logical, options, blocked, problems,
              fw_in_fastbootd: bool = False, bootloader_images=()) -> list[Step]:
    # a phone whose bootloader refuses some firmware (OnePlus 13: xbl ...) and whose fastbootd refuses
    # other firmware (modem): each image goes in from the bootloader if it takes it, else from fastbootd
    def kind_for(name: str) -> str:
        if not fw_in_fastbootd:
            return "flash"
        if bootloader_images:  # known split (Super Flasher's): bootloader list first, the rest from fastbootd
            return "flash-any" if name in bootloader_images else "flash-fw"
        return "flash-any"
    physical = ([Step(kind_for(n), n) for n in rom_physical]
                + [Step(kind_for(n), n, source="backup") for n in restore_physical])
    physical.sort(key=lambda st: (AVB_ORDER_LAST.index(st.partition) + 1) if st.partition in AVB_ORDER_LAST else 0)
    steps = [Step("to-bootloader"), Step("preflight")]
    # The wipe runs first, while the bootloader is still the phone's own. A region conversion writes a
    # new abl, and a ColorOS / OxygenOS 17 abl is reported to leave bootloader fastboot within a second
    # on Chinese OnePlus 13 hardware, so a wipe planned after it might never run. Every later step
    # (fastbootd, reboot) doesn't need the bootloader's fastboot mode.
    if options.wipe_data:
        steps.append(Step("wipe"))
    if fw_in_fastbootd and physical:
        # the bootloader writes what it accepts; what it refuses is written from fastbootd right after
        # the reboot there (a whole super.img goes in from the bootloader first)
        if super_image:
            steps.append(Step("flash-super", "super", detail="super.img replaces every logical partition, including my_region"))
        logical = [Step("flash-logical", n) for n in rom_logical] + [Step("flash-logical", n, source="backup") for n in restore_logical]
        # no separate consent: this phone can't be flashed any other way (firmware known to break
        # fastbootd is still refused)
        if "fastbootd" in blocked:
            problems.append("This phone writes some firmware from fastbootd, which is known to be broken on this firmware.")
        in_bootloader = [st for st in physical if st.kind == "flash-any"]
        in_fastbootd = [st for st in physical if st.kind == "flash-fw"]
        # before the system partitions: the other slot's copies and leftover OTA "-cow" partitions out of
        # super, so the new images have room (as Super Flasher does)
        free = [Step("free-super", flags=[st.partition for st in logical])] if logical else []
        steps += [*in_bootloader, Step("to-fastbootd"), Step("snapshot"), *in_fastbootd, *free, *logical]
        steps.append(Step("reboot-from-fastbootd" if options.reboot_after else "back-to-bootloader"))
        return steps
    steps += physical
    if super_image:
        steps.append(Step("flash-super", "super", detail="super.img replaces every logical partition, including my_region"))

    logical = [Step("flash-logical", n) for n in rom_logical] + [Step("flash-logical", n, source="backup") for n in restore_logical]
    if logical:
        names = ", ".join(st.partition for st in logical[:4]) + ("..." if len(logical) > 4 else "")
        if "fastbootd" in blocked:
            problems.append(f"Flashing {names} needs fastbootd, which is known to be broken on this firmware.")
        elif not options.allow_fastbootd:
            hint = " or use a ROM folder that includes super.img" if rom_logical else ""
            problems.append(f"Flashing {names} (logical partitions) needs fastbootd. Allow fastbootd{hint}.")
        else:
            steps += [Step("to-fastbootd"), Step("snapshot"), *logical]
            # finish from fastbootd: going back to the bootloader would go through the new abl
            steps.append(Step("reboot-from-fastbootd" if options.reboot_after else "back-to-bootloader"))
            return steps
    if options.reboot_after:
        steps.append(Step("reboot"))
    return steps


def prepare_restore(device: DeviceState, backup_folder: Path | None, options: FlashOptions) -> FlashPlan:
    """Restore only the region files from a backup (e.g. after an OTA replaced them). No ROM involved."""
    problems = check_backup(backup_folder, device.facts, device.profile)
    blocked = device.profile.blocked_actions(device.props)
    if "bootloader" in blocked:
        problems.append("This phone's current firmware is known to break the bootloader's fastboot mode. Restoring is disabled.")
    if not options.restore_from_backup:
        problems.append("Choose at least one region file to restore.")
    if options.include_optional or options.wipe_data:
        problems.append("A restore only writes region files from your backup.")
    restore_physical, restore_logical = _restore_targets(options, device.profile, backup_folder, set(), problems)
    steps = _assemble([], [], False, restore_physical, restore_logical, options, blocked, problems,
                      fw_in_fastbootd=device.profile.firmware_in_fastbootd,
                      bootloader_images=device.profile.bootloader_images)
    return FlashPlan(
        rom_path="",
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
    return [s.partition for s in plan.steps if s.kind in ("flash", "flash-fw", "flash-any", "flash-logical", "flash-super") and s.source == "rom"]


def verified_step_images(plan: FlashPlan) -> None:
    """Explicit image files (root assistant) must still match the hash recorded when they were checked."""
    for step in plan.steps:
        if step.image and step.status != "done":
            path = Path(step.image)
            if not path.is_file() or sha256_file(path) != step.sha256:
                raise FlashError(f"{path.name} changed since it was checked (hash mismatch). Nothing was written.")


def verified_backup_files(plan: FlashPlan, emit: EventFn) -> dict[str, Path]:
    """Check every backup image we'll write against the hash recorded when it was made."""
    files: dict[str, Path] = {}
    for step in plan.steps:
        if step.source != "backup" or step.status == "done":
            continue
        images = backup_images(plan.options.network_backup or plan.backup, step.partition)
        if not images:
            raise FlashError(f"The backup no longer has {step.partition}.")
        for full, (path, sha, _src) in images.items():
            emit({"type": "log", "message": f"Checking backup image {path.name} ..."})
            if not path.exists() or sha256_file(path) != sha:
                raise FlashError(f"Backup image {path.name} doesn't match the hash recorded in its manifest. Nothing was written.")
            files[full] = path
    return files


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
        if img.file is not None and not img.pieces:
            files[name] = img.file  # fastboot takes raw and sparse images as they are
        else:
            target = workdir / f"{name}.img"
            if not (target.exists() and target.stat().st_size == img.size):
                materialize(rom, name, target)  # payload, zip entry, split super, inside super.img
            files[name] = target
    return files


class Runner:
    def __init__(
        self,
        plan: FlashPlan,
        rom: RomInfo | None,
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
        self.backup_files: dict[str, Path] = {}

    # --- mode handling ------------------------------------------------

    def _log_fastboot(self) -> None:
        """Which fastboot does the work: an old copy on the PATH is a classic cause of USB trouble."""
        try:
            res = self.fastboot_tool.run("--version", timeout=15)
            version = (res.stdout or res.stderr).strip().splitlines()[:1]
        except OSError:
            version = []
        where = " ".join(self.fastboot_tool.prefix)
        self.emit({"type": "log", "message": f"fastboot: {where}" + (f" ({version[0]})" if version else "")})

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

    def _still_there(self) -> None:
        """Before each write: the phone we were talking to must still be in fastboot. Without this, a
        phone that dropped off USB (or restarted) leaves fastboot waiting for it for half an hour."""
        if self.fb is None:
            return
        if self.fb.tool.serial not in fastboot_serials(self.fastboot_tool):
            self.fb = None
            raise LostPhone("the phone is no longer in fastboot")

    def _ensure_any(self) -> Fastboot:
        """flash-any: the phone in the bootloader or in fastbootd, whichever it is in."""
        self._still_there()
        if self.fb is None:
            try:
                self.fb = self._connect_now()
            except FastbootError:
                if not self._adb_in_android():
                    raise
                self._to_bootloader()  # resuming after the phone restarted to Android
        return self.fb

    def _ensure(self, mode: str) -> Fastboot:
        """Before each step: the phone must already be in the expected mode."""
        self._still_there()
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

    def _flash_physical(self, fb: Fastboot, name: str, image_for) -> str:
        # some bootloaders don't answer has-slot for every partition: an existing name_a also means A/B
        slotted = fb.has_slot(name) or fb.partition_size(f"{name}_a") is not None
        targets = [f"{name}_a", f"{name}_b"] if slotted else [name]
        chosen = {t: image_for(t) for t in targets}
        unchecked = []
        for target, image in chosen.items():
            size = image.stat().st_size
            part_size = fb.partition_size(target)
            if part_size is None:
                # some fastbootd builds don't answer partition-size for every partition (OnePlus 13: modem).
                # Ask whether it exists at all; if so, fastboot checks the size itself when writing.
                if fb.is_logical(target) is None and not fb.has_slot(name):
                    raise FlashError(f"{target} does not exist on this phone")
                unchecked.append(target)
                continue
            if size > part_size:
                raise FlashError(f"{image.name} ({size} bytes) is larger than {target} ({part_size} bytes)")
        for target, image in chosen.items():
            fb.flash(target, image)
        note = f" (size checked by fastboot: {', '.join(unchecked)})" if unchecked else ""
        return " + ".join(targets) + note

    def _backup_image(self, name: str, target: str) -> Path:
        """The backup image for a target, falling back to the other slot's copy."""
        slot = self.plan.device.get("slot_suffix", "")
        for candidate in (target, name + slot, name, f"{name}_a", f"{name}_b"):
            if candidate in self.backup_files:
                return self.backup_files[candidate]
        raise FlashError(f"No backup image for {target}")

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
            try:  # resuming after the reboot already happened: nothing to do
                if self._connect_now().mode() == "fastbootd":
                    self.fb = self._connect_now()
                    return "already in fastbootd"
            except FastbootError:
                pass
            fb = self._ensure("bootloader")
            fb.reboot("fastboot")
            self._connect(userspace=True)
            return "in fastbootd"

        fb = self._ensure(step.mode) if step.mode else self._ensure_any()
        if step.kind == "preflight":
            unlocked = ""
            for _ in range(3):  # a bootloader that just came up can miss the first question
                unlocked = fb.getvar("unlocked") or ""
                if unlocked:
                    break
                time.sleep(1)
            if unlocked != "yes":
                raise FlashError("The bootloader is locked. Flashing is not possible." if unlocked == "no" else
                                 "The bootloader didn't say whether it's unlocked. Nothing was written; try again.")
            return f"unlocked, current slot {fb.getvar('current-slot') or '?'}"
        if step.kind in ("flash", "flash-fw", "flash-any"):
            if step.source == "backup":
                return "restored " + self._flash_physical(fb, step.partition, lambda t: self._backup_image(step.partition, t))
            return "wrote " + self._flash_physical(fb, step.partition, lambda t: files[step.partition])
        if step.kind == "flash-current":
            image = Path(step.image)
            if sha256_file(image) != step.sha256:
                raise FlashError(f"{image.name} changed since it was checked; not writing it")
            slot = fb.getvar("current-slot")
            target = f"{step.partition}_{slot}" if slot in ("a", "b") and fb.has_slot(step.partition) else step.partition
            part_size = fb.partition_size(target)
            if part_size is None:
                raise FlashError(f"{target} does not exist on this phone")
            if image.stat().st_size > part_size:
                raise FlashError(f"{image.name} is larger than {target}")
            fb.flash(target, image)
            return f"wrote {target}"
        if step.kind == "flash-image":
            image = Path(step.image)
            if sha256_file(image) != step.sha256:
                raise FlashError(f"{image.name} changed since it was checked; not writing it")
            targets = [step.partition]
            if fb.has_slot(step.partition):
                cur = fb.getvar("current-slot")
                if cur not in ("a", "b"):
                    raise FlashError("Can't read the current slot from the bootloader.")
                other = "b" if cur == "a" else "a"
                slots = {"current": [cur], "other": [other], "both": [cur, other]}[step.slot]
                targets = [f"{step.partition}_{s}" for s in slots]
            for target in targets:
                part_size = fb.partition_size(target)
                if part_size is None:
                    raise FlashError(f"{target} does not exist on this phone")
                if image.stat().st_size > part_size:
                    raise FlashError(f"{image.name} is larger than {target}")
            for target in targets:
                fb.flash(target, image, flags=tuple(step.flags))
            return "wrote " + ", ".join(targets)
        if step.kind == "flash-super":
            size = files["super"].stat().st_size
            super_size = fb.partition_size("super")
            if super_size is None or size > super_size:
                raise FlashError(f"super.img ({size} bytes) does not fit super ({super_size})")
            fb.flash("super", files["super"])
            return "wrote super"
        if step.kind == "free-super":
            slot = fb.getvar("current-slot")
            if slot not in ("a", "b"):
                return "skipped: current slot unknown"
            other = "b" if slot == "a" else "a"
            removed = []
            for name in step.flags:
                for target in (f"{name}_{other}", f"{name}_{slot}-cow", f"{name}_{other}-cow"):
                    if fb.delete_logical(target):
                        removed.append(target)
            return "removed " + (", ".join(removed) if removed else "nothing (already free)")
        if step.kind == "snapshot":
            status = fb.getvar("snapshot-update-status")
            if status and status != "none":
                fb.cancel_snapshot_update()
                return f"cancelled ({status})"
            return "none pending"
        if step.kind == "flash-logical":
            if step.source == "backup":
                fb.flash(step.partition, self._backup_image(step.partition, step.partition))
                return f"restored {step.partition} from your backup (current slot)"
            fb.flash(step.partition, files[step.partition])
            return f"wrote {step.partition} (current slot)"
        if step.kind == "wipe":
            fb.wipe_data()
            return "userdata and metadata erased"
        if step.kind in ("reboot", "reboot-from-fastbootd"):
            fb.reboot()
            return "rebooting to Android"
        raise FlashError(f"unknown step {step.kind}")

    def run(self) -> FlashPlan:
        plan = self.plan
        if not plan.ok:
            raise FlashError("Flashing is blocked:\n- " + "\n- ".join(plan.problems))
        rom_names = _image_names(plan)
        if rom_names and self.rom is None:
            raise FlashError("This plan needs the ROM, but none was given.")
        self._log_fastboot()
        files = extract_images(self.rom, rom_names, self.workdir, self.emit, self.cancel) if rom_names else {}
        self.backup_files = verified_backup_files(plan, self.emit)
        verified_step_images(plan)
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
                if step.kind == "to-fastbootd":
                    self._run_deferred(files, total)
            except LostPhone as exc:
                self._fail(step, index, exc, lost=True)
            except FastbootError as exc:
                if step.kind == "flash-any" and self.fb is not None and self.fb.mode() == "bootloader":
                    # the bootloader refused it: written from fastbootd after the reboot there
                    step.status, step.detail = "deferred", f"the bootloader refused it ({exc}); written from fastbootd"
                    save_journal(plan, self.journal)
                    self.emit({"type": "step_done", "index": index, "label": step.label, "status": "deferred",
                               "detail": step.detail})
                    continue
                self._fail(step, index, exc)
            except (FlashError, OSError) as exc:
                self._fail(step, index, exc)
            save_journal(plan, self.journal)
            self.emit({"type": "step_done", "index": index, "label": step.label, "status": "done", "detail": step.detail})
        self.emit({"type": "finished", "journal": str(self.journal)})
        return plan

    def _run_deferred(self, files, total: int) -> None:
        """Right after the reboot to fastbootd: what the bootloader refused."""
        for step in self.plan.steps:
            if step.status != "deferred":
                continue
            index = self.plan.steps.index(step) + 1
            self.emit({"type": "step_start", "index": index, "total": total, "label": step.label + " (from fastbootd)"})
            try:
                step.detail = self._run_step(step, files) + " (from fastbootd)"
                step.status = "done"
            except LostPhone as exc:
                self._fail(step, index, exc, lost=True)
            except (FastbootError, FlashError, OSError) as exc:
                self._fail(step, index, exc)
            save_journal(self.plan, self.journal)
            self.emit({"type": "step_done", "index": index, "label": step.label, "status": "done", "detail": step.detail})

    def _fail(self, step: Step, index: int, exc: BaseException, lost: bool = False):
        step.status = "failed"
        step.detail = str(exc)
        save_journal(self.plan, self.journal)
        self.emit({"type": "step_done", "index": index, "label": step.label, "status": "failed", "detail": step.detail})
        if lost:
            raise FlashError(f"Step {index} ({step.label}) failed: {exc}\n{USB_LOST}\nJournal: {self.journal}") from exc
        raise FlashError(
            f"Step {index} ({step.label}) failed: {exc}\n"
            "Do not reboot or unplug the phone. Fix the cause, then resume from the journal: "
            f"{self.journal}"
        ) from exc


USB_LOST = (
    "The phone dropped off USB (or restarted) in the middle of this step, so this write didn't finish; nothing "
    "after it was written. That's the USB link, not the ROM. Plug the phone straight into a port on the PC itself "
    "(a rear port; USB 2.0 if there is one; no hub, extension or front-panel port), use a good data cable, and keep "
    "the PC from sleeping. Then put the phone back in the bootloader and press Resume."
)


def resume_runner(journal: Path, profile: Profile, adb: Tool, fastboot: Tool, **kwargs) -> Runner:
    plan = load_journal(journal)
    for step in plan.steps:
        if step.status == "failed":
            step.status = "pending"
    if any(st.status == "deferred" for st in plan.steps):
        # what the bootloader refused still needs fastbootd: go there again (a no-op if it's there)
        for st in plan.steps:
            if st.kind == "to-fastbootd":
                st.status = "pending"
    rom = open_rom(plan.rom_path) if plan.rom_path else None
    return Runner(plan, rom, profile, adb, fastboot, journal, Path(journal).parent / "images", **kwargs)
