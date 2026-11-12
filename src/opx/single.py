"""Flash one image (boot, init_boot, recovery, vbmeta, dtbo, ...) with checks.

The plan runs through the same Runner as every other flash: reboot to the
bootloader, check it's unlocked, re-hash the image, check it fits, write it.

Refused: never-flash partitions (identity and personal data), partitions inside
`super` (those need fastbootd; use a full ROM flash), images whose type doesn't
match the partition (e.g. a vbmeta into boot), images larger than the
partition, and firmware known to break the bootloader. A backup of this phone
is required, as for every flash.
"""

from __future__ import annotations

from pathlib import Path

from opx.flash import FlashOptions, FlashPlan, Step, check_backup, sha256_file
from opx.plan import DeviceState
from opx.remote import check_name

SLOTS = ("current", "other", "both")

# First bytes of each image type.
BOOT = b"ANDROID!"
VENDOR_BOOT = b"VNDRBOOT"
VBMETA = b"AVB0"
DTBO = bytes.fromhex("d7b7ab1e")
KINDS = {
    "boot": BOOT, "init_boot": BOOT, "recovery": BOOT,
    "vendor_boot": VENDOR_BOOT, "vendor_kernel_boot": VENDOR_BOOT,
    "dtbo": DTBO,
}
VERITY_FLAGS = ("--disable-verity", "--disable-verification")


def expected_magic(partition: str) -> bytes | None:
    if partition.startswith("vbmeta"):
        return VBMETA
    return KINDS.get(partition)


def check_image(partition: str, image: Path) -> tuple[list[str], list[str]]:
    """(problems, warnings) about the image file itself."""
    problems, warnings = [], []
    if not image.is_file():
        return [f"Image not found: {image}"], []
    if image.stat().st_size == 0:
        return ["The image file is empty."], []
    with open(image, "rb") as fh:
        head = fh.read(8)
    magic = expected_magic(partition)
    if magic is None:
        warnings.append(f"OPxFlasher can't check what kind of image this is for {partition}; "
                        "make sure it's the right file.")
    elif not head.startswith(magic):
        found = next((name for name, m in (("boot", BOOT), ("vendor_boot", VENDOR_BOOT),
                                            ("vbmeta", VBMETA), ("dtbo", DTBO)) if head.startswith(m)), None)
        what = f"a {found} image" if found else "not a known image type"
        problems.append(f"This file is {what}, not a {partition} image. Pick the right file.")
    if partition in ("boot", "init_boot", "vendor_boot"):
        warnings.append(f"A {partition} image must be from your phone's exact build "
                        "(or patched from it), or the phone may not boot.")
    return problems, warnings


def prepare_image_flash(
    device: DeviceState,
    backup_folder: Path | None,
    partition: str,
    image: Path,
    slot: str = "current",
    disable_verity: bool = False,
    reboot_after: bool = True,
) -> tuple[FlashPlan, list[str]]:
    """Returns the plan and warnings to show the user (plan.problems block it)."""
    check_name(partition)
    image = Path(image)
    problems = check_backup(backup_folder, device.facts)
    warnings: list[str] = []
    if slot not in SLOTS:
        raise ValueError(slot)

    blocked = device.profile.blocked_actions(device.props)
    if "bootloader" in blocked:
        problems.append("This phone's current firmware is known to break the bootloader's fastboot mode. "
                        "Flashing is disabled.")
    if device.profile.is_never_flash(partition):
        problems.append(f"{partition} is never flashed: it holds this phone's identity or your personal data.")
    if partition in device.logical or partition == "super":
        problems.append(f"{partition} is inside 'super' and needs fastbootd. Use a full ROM flash for it.")
    p, w = check_image(partition, image)
    problems += p
    warnings += w
    size = device.size_of(partition)
    if size is not None and image.is_file() and image.stat().st_size > size:
        problems.append(f"The image ({image.stat().st_size:,} bytes) is larger than {partition} ({size:,} bytes).")
    if disable_verity and not partition.startswith("vbmeta"):
        problems.append("Disabling verity only applies to vbmeta images.")
    if disable_verity:
        warnings.append("Verity and verification will be disabled. Needed by some custom kernels; "
                        "it also turns off a protection, and an OTA update may undo it.")
    if slot != "current":
        warnings.append("Flashing a slot other than the current one only matters if you switch slots later."
                        if slot == "other" else "Both slots will get this image.")

    sha = sha256_file(image) if image.is_file() and not problems else ""
    steps = [
        Step("to-bootloader"),
        Step("preflight"),
        Step("flash-image", partition, source="file", image=str(image), sha256=sha, slot=slot,
             flags=list(VERITY_FLAGS) if disable_verity else []),
    ]
    if reboot_after:
        steps.append(Step("reboot"))
    plan = FlashPlan(
        rom_path="",
        device=dict(device.facts),
        options=FlashOptions(reboot_after=reboot_after),
        steps=steps,
        problems=problems,
        blocked_actions=sorted(blocked),
        backup=str(backup_folder) if backup_folder else None,
    )
    return plan, warnings
