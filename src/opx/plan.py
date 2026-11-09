"""Phase 2: compare a ROM with the phone and produce a dry-run flash plan.

Nothing here writes to the phone. The plan answers two questions:
  1. Is it safe to flash this ROM on this phone? (checks -> verdict)
  2. What exactly would be written where? (plan items)

Fail closed: any check that can't be answered blocks the plan.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from opx import profile as profiles
from opx.backup import device_facts
from opx.profile import Profile
from opx.remote import BY_NAME_DIRS, MAPPER_DIR
from opx.rom import avb, qcom
from opx.rom.ota import RomError, RomInfo
from opx.rom.payload import PayloadError
from opx.session import Session

PASS, INFO, WARN, BLOCK = "pass", "info", "warn", "block"


# --- what we know about the phone ------------------------------------------


@dataclass
class DeviceState:
    source: str  # "live", "backup", "none"
    label: str
    facts: dict[str, str]
    props: dict[str, str]
    profile: Profile
    sizes: dict[str, int] = field(default_factory=dict)  # physical partition sizes
    logical: set[str] = field(default_factory=set)  # logical names without slot suffix
    reader: Callable[[str], bytes | None] = field(default=lambda name: None, repr=False)
    size_lookup: Callable[[str], int | None] | None = field(default=None, repr=False)

    @property
    def slot(self) -> str:
        return self.facts.get("slot_suffix", "")

    def size_of(self, name: str) -> int | None:
        for candidate in (name + self.slot, name, name + "_a"):
            if candidate in self.sizes:
                return self.sizes[candidate]
        if self.size_lookup:
            return self.size_lookup(name)
        return None

    def read_current(self, name: str) -> bytes | None:
        return self.reader(name)


def device_from_session(session: Session) -> DeviceState:
    shell = session.shell
    facts = device_facts(session.props)
    slot = facts.get("slot_suffix", "")
    by_name = next((d for d in BY_NAME_DIRS if shell.list_dir(d)), BY_NAME_DIRS[0])
    physical = set(shell.list_dir(by_name))
    logical = {n[:-2] if n.endswith(("_a", "_b")) else n for n in shell.list_dir(MAPPER_DIR) if n != "control"}
    cache: dict[str, int | None] = {}

    def path_for(name: str) -> str | None:
        for candidate in (name + slot, name):
            if candidate in physical:
                return f"{by_name}/{candidate}"
        return None

    def reader(name: str) -> bytes | None:
        path = path_for(name)
        return shell.read_small(path) if path else None

    def size_lookup(name: str) -> int | None:
        if name not in cache:
            path = path_for(name)
            cache[name] = shell.size_of(path) if path else None
        return cache[name]

    return DeviceState(
        source="live",
        label=f"{facts.get('model')} ({session.serial}), connected",
        facts=facts,
        props=session.props,
        profile=session.profile,
        logical=logical,
        reader=reader,
        size_lookup=size_lookup,
    )


def device_from_backup(folder: Path) -> DeviceState:
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
    facts = manifest.get("device", {})
    props = {"ro.product.model": facts.get("model", ""), "ro.build.display.id": facts.get("build", "")}
    slot = facts.get("slot_suffix", "")
    sizes, logical, files = {}, set(), {}
    for part in manifest.get("partitions", []):
        if part.get("status") == "failed":
            continue
        files[part["name"]] = folder / part["file"]
        if part.get("source") == "mapper":
            n = part["name"]
            logical.add(n[:-2] if n.endswith(("_a", "_b")) else n)
        else:
            sizes[part["name"]] = part["size"]

    def reader(name: str) -> bytes | None:
        for candidate in (name + slot, name):
            f = files.get(candidate)
            if f and f.exists():
                return f.read_bytes()
        return None

    return DeviceState(
        source="backup",
        label=f"{facts.get('model')} backup ({manifest.get('mode')}, {manifest.get('created_utc', '')[:10]})",
        facts=facts,
        props=props,
        profile=profiles.load(manifest.get("profile") or "oplus-generic"),
        sizes=sizes,
        logical=logical,
        reader=reader,
    )


def no_device() -> DeviceState:
    return DeviceState(source="none", label="No phone or backup selected", facts={}, props={}, profile=profiles.load("oplus-generic"))


# --- the report -------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class PlanItem:
    partition: str
    size: int
    action: str  # "flash", "super", "conditional", "skip"
    target: str
    device_size: int | None = None
    note: str = ""


@dataclass
class Report:
    rom: dict[str, str]
    device: dict[str, str]
    checks: list[Check]
    items: list[PlanItem]

    @property
    def verdict(self) -> str:
        if any(c.status == BLOCK for c in self.checks):
            return "BLOCKED"
        if any(c.status == WARN for c in self.checks):
            return "OK WITH WARNINGS"
        return "OK"

    def to_json(self) -> str:
        return json.dumps({"verdict": self.verdict, **asdict(self)}, indent=2)


def rom_summary(rom: RomInfo) -> dict[str, str]:
    return {
        "path": str(rom.path),
        "kind": rom.kind,
        "codename": rom.codename,
        "product": rom.product,
        "android": rom.android,
        "security_patch": rom.security_patch,
        "build_incremental": rom.build_incremental,
        "full": {True: "yes", False: "no (incremental)", None: "unknown"}[rom.is_full],
        "images": str(len(rom.images)),
    }


def _avb_index(data: bytes | None) -> int | None:
    if not data or not avb.has_avb(data):
        return None
    try:
        return avb.parse(data).rollback_index
    except avb.AvbError:
        return None


def _qcom_arb(data: bytes | None) -> qcom.QcomArb | None:
    if not data:
        return None
    try:
        return qcom.read_arb(data)
    except qcom.QcomError:
        return None


def build_report(rom: RomInfo, dev: DeviceState) -> Report:
    checks: list[Check] = []
    prof = dev.profile
    add = lambda name, status, detail: checks.append(Check(name, status, detail))  # noqa: E731

    # 1. package type
    if rom.is_full is False:
        add("Package", BLOCK, "Incremental OTA. Only full OTAs or full image sets can be flashed.")
    elif rom.is_full is None:
        add("Package", BLOCK, "Can't tell whether this is a full OTA.")
    else:
        add("Package", PASS, f"Full package ({rom.kind}, {len(rom.images)} images).")

    # 2. which phone
    if dev.source == "none":
        add("Phone", BLOCK, "Connect the phone (rooted, over ADB) or choose a backup so the ROM can be compared.")
    else:
        add("Phone", INFO, dev.label)

    dev_codename = dev.facts.get("codename", "")
    if not rom.codename:
        add("Device match", BLOCK, "The ROM doesn't say which device it is for (no OTA metadata).")
    elif not dev_codename:
        add("Device match", BLOCK, f"ROM is for '{rom.codename}', but the phone's codename is unknown.")
    elif rom.codename != dev_codename:
        add("Device match", BLOCK, f"ROM is for '{rom.codename}', this phone is '{dev_codename}'.")
    else:
        add("Device match", PASS, f"Both are '{rom.codename}'.")
        dev_product = dev.facts.get("product", "")
        if rom.product and dev_product and rom.product != dev_product:
            add(
                "Region",
                WARN,
                f"Region/variant change: {dev_product} -> {rom.product}. "
                "Mobile network may need your own backed-up oplusstanvbk and my_region.",
            )

    # 3. firmware already known to be dangerous on the phone
    for issue in prof.issues_for(dev.props):
        add("Known issue", BLOCK if issue.severity == "danger" else WARN, issue.message)

    # 4. security patch direction (informational)
    dev_patch = dev.facts.get("security_patch", "")
    if rom.security_patch and dev_patch:
        if rom.security_patch < dev_patch:
            add("Security patch", WARN, f"Downgrade: ROM {rom.security_patch}, phone {dev_patch}. Check anti-rollback below.")
        else:
            add("Security patch", PASS, f"ROM {rom.security_patch}, phone {dev_patch}.")

    # 5. AVB rollback indexes
    for name in prof.avb_images:
        if not rom.image(name):
            continue
        try:
            rom_idx = _avb_index(rom.read_image(name))
        except (RomError, PayloadError) as e:
            add(f"AVB {name}", BLOCK, f"Couldn't read {name} from the ROM: {e}")
            continue
        dev_idx = _avb_index(dev.read_current(name)) if dev.source != "none" else None
        if rom_idx is None:
            add(f"AVB {name}", BLOCK, f"No AVB metadata in the ROM's {name}.")
        elif dev_idx is None:
            add(f"AVB {name}", BLOCK, f"ROM rollback index {rom_idx}; the phone's current {name} couldn't be read.")
        elif rom_idx < dev_idx:
            add(f"AVB {name}", BLOCK, f"ROM rollback index {rom_idx} is lower than the phone's {dev_idx}: verified boot would reject it.")
        else:
            add(f"AVB {name}", PASS, f"ROM rollback index {rom_idx} >= phone {dev_idx}.")

    # 6. Qualcomm anti-rollback (experimental)
    for name in prof.qcom_arb_images:
        if not rom.image(name):
            continue
        try:
            rom_arb = _qcom_arb(rom.read_image(name))
        except (RomError, PayloadError) as e:
            add(f"Qualcomm ARB {name}", BLOCK, f"Couldn't read {name} from the ROM: {e}")
            continue
        dev_arb = _qcom_arb(dev.read_current(name)) if dev.source != "none" else None
        rv = rom_arb.value if rom_arb else None
        dv = dev_arb.value if dev_arb else None
        values = f"ROM {rv if rv is not None else '?'}, phone {dv if dv is not None else '?'}"
        if not prof.qcom_arb_validated:
            add(
                f"Qualcomm ARB {name}",
                BLOCK,
                f"Reader not yet validated for {prof.name} ({values}). "
                "Confirm these numbers against known values before trusting them.",
            )
        elif rv is None or dv is None:
            add(f"Qualcomm ARB {name}", BLOCK, f"Couldn't read the anti-rollback version ({values}).")
        elif rv < dv:
            add(f"Qualcomm ARB {name}", BLOCK, f"ROM ARB is lower than the phone's ({values}): this would hard-brick.")
        else:
            add(f"Qualcomm ARB {name}", PASS, values)

    # 7. plan items and sizes
    items: list[PlanItem] = []
    logical = (rom.payload.logical_names if rom.payload else set()) | dev.logical
    too_big = []
    for img in rom.images:
        is_logical = img.logical if img.logical is not None else img.name in logical
        if prof.is_never_flash(img.name):
            items.append(PlanItem(img.name, img.size, "skip", "-", note="never flashed (device identity / data)"))
            continue
        if is_logical:
            items.append(PlanItem(img.name, img.size, "super", "super (logical)"))
            continue
        device_size = dev.size_of(img.name) if dev.source != "none" else None
        action = "conditional" if prof.is_conditional(img.name) else "flash"
        note = "off unless you choose it (region / signal files)" if action == "conditional" else ""
        if device_size is not None and img.size > device_size:
            too_big.append(img.name)
            note = "image is larger than the partition"
        items.append(PlanItem(img.name, img.size, action, f"{img.name}_a + {img.name}_b", device_size, note))

    if too_big:
        add("Partition sizes", BLOCK, "Larger than the phone's partition: " + ", ".join(too_big))
    elif dev.source != "none":
        unknown = [i.partition for i in items if i.action in ("flash", "conditional") and i.device_size is None]
        if unknown:
            add("Partition sizes", WARN, "Sizes not known for: " + ", ".join(unknown))
        else:
            add("Partition sizes", PASS, "Every image fits its partition.")

    if rom.payload and rom.payload.groups and dev.source != "none":
        super_size = dev.size_of("super")
        needed = sum(g.size for g in rom.payload.groups)
        if super_size is not None and needed > super_size:
            add("Super", BLOCK, f"ROM's dynamic groups need {needed} bytes, super is {super_size}.")

    skipped = [i.partition for i in items if i.action == "skip"]
    if skipped:
        add("Protected partitions", INFO, "Will never be flashed: " + ", ".join(skipped))
    for note in rom.notes:
        add("ROM", WARN, note)

    return Report(rom=rom_summary(rom), device=dict(dev.facts), checks=checks, items=items)
