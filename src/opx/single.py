"""Flash one image (boot, init_boot, recovery, vbmeta, dtbo, ...) with checks.

The plan runs through the same Runner as every other flash: reboot to the
bootloader, check it's unlocked, re-hash the image, check it fits, write it.

Refused: never-flash partitions (identity and personal data), partitions inside
`super` (those need fastbootd; use a full ROM flash), images whose type doesn't
match the partition (e.g. a vbmeta into boot), images larger than the
partition, firmware known to break the bootloader, and the bootloader chain
itself (abl, xbl, tz ...: an image from another build can trip Qualcomm
anti-rollback, and a phone that can't start its bootloader needs an authorised
EDL repair). A backup of this phone is required unless the user explicitly
skips it (typed NO BACKUP): only this one image is written, and everything
above is still refused.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from opx.flash import FlashOptions, FlashPlan, Step, backup_problems, sha256_file
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

# Qualcomm boot-chain and trusted firmware: never flashed one at a time. Each carries an anti-rollback
# version OPxFlasher can't check against the phone yet, and these are what a phone starts before the
# bootloader's fastboot mode, so a bad one can't be fixed with fastboot.
BOOT_CHAIN = ("abl", "xbl", "xbl_*", "tz", "hyp", "devcfg", "aop", "aop_*", "uefi", "uefisecapp", "keymaster",
              "qupfw", "cpucp", "cpucp_*", "shrm", "imagefv", "rpm", "pmic", "storsec", "featenabler",
              "multiimgoem", "multiimgqti", "tzsc", "secdata", "apdp", "msadp", "hwfw")


def boot_chain(partition: str, extra=()) -> bool:
    base = partition[:-2] if partition.endswith(("_a", "_b")) else partition
    return any(fnmatch.fnmatchcase(base.lower(), pat) for pat in (*BOOT_CHAIN, *extra))


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
    skip_backup: bool = False,
) -> tuple[FlashPlan, list[str]]:
    """Returns the plan and warnings to show the user (plan.problems block it). `skip_backup`: the user
    typed NO BACKUP, so a missing or unusable backup doesn't block this one image."""
    check_name(partition)
    image = Path(image)
    warnings: list[str] = []
    problems = backup_problems(backup_folder, device, skip_backup, warnings)
    if slot not in SLOTS:
        raise ValueError(slot)

    blocked = device.profile.blocked_actions(device.props)
    if "bootloader" in blocked:
        problems.append("This phone's current firmware is known to break the bootloader's fastboot mode. "
                        "Flashing is disabled.")
    if device.profile.is_never_flash(partition):
        problems.append(f"{partition} is never flashed: it holds this phone's identity or your personal data.")
    if boot_chain(partition, device.profile.qcom_arb_images):
        problems.append(f"{partition} is part of the bootloader chain and isn't flashed on its own: one from another "
                        "build can trip anti-rollback, and a phone that can't start its bootloader can't be fixed "
                        "with fastboot. It's only written as part of a full ROM, after the ROM check.")
    # without root the phone's own list of logical partitions can't be read: known names stand in
    from opx.plan import SUPER_NAMES

    logical = device.logical or set(SUPER_NAMES)
    if partition in logical or partition == "super" or (not device.logical and partition.startswith("my_")):
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
