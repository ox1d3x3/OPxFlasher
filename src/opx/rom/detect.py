"""What is this file? A plain-language report on any ROM input.

Recognises full and incremental OTA zips, payload.bin, image folders, image zips
(fastboot flasher kits, AERA image zips), split / sparse / super images, OPxFlasher
full-ROM packages, and encrypted factory formats (named, not opened). Says which
phone and build it is for, what's inside, what's missing, and whether it can be
flashed by OPxFlasher or converted to a full ROM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from opx.rom.ota import RomError, RomInfo, open_rom

# partitions every full Android ROM of this generation has; missing ones are worth a warning
CORE_PHYSICAL = ("boot", "vendor_boot", "dtbo", "vbmeta")
CORE_LOGICAL = ("system", "system_ext", "product", "vendor", "odm")

KIND_LABELS = {
    "ota-zip": "Full OTA zip (payload.bin)",
    "payload": "payload.bin",
    "folder": "Image folder (fastboot images)",
    "image-zip": "Image zip (fastboot images)",
    "fullrom": "OPxFlasher full ROM",
}


@dataclass
class Detection:
    path: Path
    ok: bool
    kind: str = ""
    label: str = ""
    problem: str = ""
    rom: RomInfo | None = None
    full: bool | None = None
    device: str = ""
    product: str = ""
    android: str = ""
    build: str = ""
    security_patch: str = ""
    built: str = ""
    profile: str = ""
    physical: list[str] = field(default_factory=list)
    logical: list[str] = field(default_factory=list)
    total_size: int = 0
    sparse: list[str] = field(default_factory=list)
    big: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    guessed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def convertible(self) -> tuple[bool, str]:
        if not self.ok:
            return False, self.problem
        if self.full is False:
            return False, "It's an incremental OTA: it only holds the changes from one build to the next."
        if not self.physical and not self.logical:
            return False, "It has no images to convert."
        return True, ""

    @property
    def flashable(self) -> bool:
        return self.ok and self.full is not False

    def lines(self) -> list[str]:
        """The report, one fact per line."""
        if not self.ok:
            return [f"Not a ROM OPxFlasher can use: {self.problem}"]
        full = "" if self.label.startswith(("Full", "Incremental")) else (
            " (full)" if self.full else " (INCREMENTAL)" if self.full is False else "")
        out = [f"Type:      {self.label}{full}"]
        who = " ".join(x for x in (self.product, f"({self.device})" if self.device else "") if x)
        out.append(f"For:       {who or 'unknown phone'}" + (f" - profile {self.profile}" if self.profile else ""))
        build = ", ".join(x for x in (self.build, f"Android {self.android}" if self.android else "",
                                     f"patch {self.security_patch}" if self.security_patch else "",
                                     f"built {self.built}" if self.built else "") if x)
        out.append(f"Build:     {build or 'unknown'}")
        out.append(f"Images:    {len(self.physical) + len(self.logical)} ({len(self.physical)} firmware / boot, "
                   f"{len(self.logical)} inside super{', guessed from names' if self.guessed else ''}), "
                   f"{self.total_size / 1e9:.2f} GB")
        if self.missing:
            out.append(f"Missing:   {', '.join(self.missing)} - a full ROM normally has these")
        if self.skipped:
            out.append(f"Never written: {', '.join(self.skipped)} (identity / personal data)")
        if self.sparse:
            out.append(f"Sparse:    {len(self.sparse)} image(s) stored sparse; converting expands them")
        ok, why = self.convertible
        out.append("Flash:     " + ("OPxFlasher can check and flash it" if self.flashable else "no"))
        out.append("Convert:   " + ("can be converted to a full ROM (recovery zip / image folder)" if ok else f"no - {why}"))
        out += [f"Note:      {n}" for n in self.notes]
        return out


def detect(path: str | Path) -> Detection:
    path = Path(path).expanduser()
    try:
        rom = open_rom(path)
    except (RomError, OSError, ValueError) as e:
        return Detection(path, ok=False, problem=str(e))
    return describe(rom)


def describe(rom: RomInfo) -> Detection:
    from opx import profile as profiles

    d = Detection(rom.path, ok=True, kind=rom.kind, rom=rom, full=rom.is_full)
    d.label = KIND_LABELS.get(rom.kind, rom.kind)
    if rom.kind == "fullrom":
        d.label += " (zip)" if rom.path.is_file() else " (folder)"
    elif rom.kind == "image-zip" and any("flasher package" in n for n in rom.notes):
        d.label = "Fastboot flasher package (zip of images)"
    if d.full is False:
        d.label = {"ota-zip": "Incremental OTA zip", "payload": "Incremental payload.bin"}.get(rom.kind, d.label)
    d.device, d.product, d.android = rom.codename, rom.product, rom.android
    d.build = rom.metadata.get("post-build-incremental", "") or rom.metadata.get("version_name", "")
    d.security_patch = rom.security_patch
    ts = rom.metadata.get("post-timestamp", "")
    if ts.isdigit():
        d.built = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")
    prof = profiles.detect({"ro.product.model": d.product}) if d.product else None
    if prof is not None and prof.id != "oplus-generic":
        d.profile = prof.name
    for img in rom.images:
        if img.name == "super" and any(i.in_super is not None for i in rom.images):
            continue  # its partitions are listed one by one
        (d.logical if img.logical else d.physical).append(img.name)
        d.total_size += img.size
        if img.sparse:
            d.sparse.append(img.name)
        if img.size >= 1 << 30:
            d.big.append(img.name)
        d.guessed = d.guessed or img.guessed
    if prof is not None:
        d.skipped = [n for n in d.physical + d.logical if prof.is_never_flash(n)]
    names = set(d.physical) | set(d.logical)
    has_super = "super" in names
    d.missing = [n for n in CORE_PHYSICAL if n not in names]
    if not has_super:
        d.missing += [n for n in CORE_LOGICAL if n not in names]
    d.notes = list(rom.notes)
    return d
