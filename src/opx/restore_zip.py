"""Build a recovery-flashable restore zip from a Full firmware backup.

Two kinds:

- ``aera``: the standard image-zip layout AERA Recovery installs natively (below).
- ``orangefox``: a normal flashable zip for TWRP-based recoveries (OrangeFox, TWRP).
  Its installer script (``opx/recovery/update-binary.sh``) checks the board, the
  serial number, the active slot and that the bootloader-chain firmware hasn't
  changed since the backup (anti-rollback), verifies every file, then writes the
  firmware images to the current slot and the whole backed-up super partition
  (stored in 1 GiB parts), and reads everything back.

The AERA zip uses the standard Android image-zip layout that AERA Recovery installs
natively ("image zip"):

    android-info.txt      require board=<codename>|<recovery names>   (wrong phone -> recovery refuses)
    <partition>.img       firmware and boot images of the backup's active slot
    <logical>.img         system, vendor, product, my_region, ... cut out of the backed-up super
    super_empty.img       layout of super (groups + partitions, no data)
    SHA256SUMS            for people, plus opxflasher-restore.json (phone, build, serial)

The recovery checks the board, every image's CRC, rebuilds only the current
slot's partitions inside super and compares anti-rollback with the phone.
What it can't check is the serial, so the zip is named after the phone and
only made from that phone's own backup. Identity partitions and personal data
are never put in it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from opx import __version__, lp
from opx.restore import RestoreError, backup_as_rom, manifest_of

Progress = Callable[[str, int, int], None]  # (what, done, total)


def _profile(model: str):
    from opx import profile as profiles

    return profiles.detect({"ro.product.model": model})


RECOVERIES = {"aera": "AERA Recovery", "orangefox": "OrangeFox / TWRP"}
CHUNK = 1 << 30
SCRIPT = Path(__file__).parent / "recovery" / "update-binary.sh"
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def _check_backup(manifest: dict, images, progress: Progress | None, cancel) -> None:
    """Every image going into the zip must still match the SHA-256 recorded when it was backed up.
    (The zip gets fresh checksums of what it holds, so a backup damaged on disk would otherwise
    travel on looking valid.)"""
    recorded = {p.get("file"): p.get("sha256") for p in manifest.get("partitions", [])}
    total = sum(i.size for i in images)
    done = 0
    for img in images:
        want = recorded.get(img.file.name)
        if not want:
            raise RestoreError(f"The backup has no recorded SHA-256 for {img.file.name}; verify the backup first.")
        h = hashlib.sha256()
        with open(img.file, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(f"Checking the backup: {img.name}", done, total)
                if cancel and cancel():
                    raise RestoreError("Cancelled.")
        if h.hexdigest() != want:
            raise RestoreError(f"{img.file.name} no longer matches the SHA-256 recorded when it was backed up: "
                               "the backup is damaged. No zip was made; use another backup.")


def zip_name(facts: dict, recovery: str = "aera") -> str:
    safe = lambda s: "".join(c if c.isalnum() or c in "._-" else "_" for c in s)
    tail = "" if recovery == "aera" else f"-{recovery}"
    return (f"OPxFlasher-restore-{safe(facts.get('model', 'phone'))}-{safe(facts.get('build', ''))}"
            f"-{safe(facts.get('serial', ''))}{tail}.zip")


def build(folder: Path, out_dir: Path, compress: bool = False,
          progress: Progress | None = None, cancel: Callable[[], bool] | None = None,
          recovery: str = "aera", chunk: int = CHUNK) -> Path:
    """Write the restore zip next to `out_dir`; returns its path."""
    if recovery not in RECOVERIES:
        raise RestoreError(f"Unknown recovery '{recovery}' (use {', '.join(RECOVERIES)}).")
    if chunk <= 0 or chunk % (1 << 20):
        raise RestoreError("Super parts must be whole MiB.")
    folder, out_dir = Path(folder), Path(out_dir)
    manifest = manifest_of(folder)
    facts = manifest.get("device", {})
    rom = backup_as_rom(folder)  # checks: Full, finished, has super; active slot only
    slot = facts.get("slot_suffix", "")
    if slot not in ("_a", "_b"):
        raise RestoreError("The backup doesn't record which slot was active.")
    codename = facts.get("codename", "")
    if not codename:
        raise RestoreError("The backup doesn't record the phone's codename.")

    super_file = rom.image("super").file
    with open(super_file, "rb") as f:
        try:
            md = lp.read_super(f, 0 if slot == "_a" else 1)
        except lp.LpError as e:
            raise RestoreError(f"Can't read the layout of super.img: {e}") from e
    logical = [p for p in md.partitions if p.name.endswith(slot) and p.size]
    layout = lp.empty_layout(md, slot)

    profile = _profile(facts.get("model", ""))
    # never identity or personal data, whatever the backup holds
    physical = [i for i in rom.images if i.name != "super" and not profile.is_never_flash(i.name)]
    boards = [codename] + [b for b in profile.recovery_boards if b != codename]
    _check_backup(manifest, [*physical, rom.image("super")], progress, cancel)
    if recovery == "orangefox":
        return _build_script_zip(folder, out_dir, manifest, facts, physical, super_file, boards,
                                 profile, compress, progress, cancel, chunk)
    total = sum(i.size for i in physical) + sum(p.size for p in logical)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / zip_name(facts)
    partial = out.with_name(out.name + ".part")
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    sums: list[str] = []
    done = 0

    def tick(what: str, n: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress(what, done, total)
        if cancel and cancel():
            raise RestoreError("Cancelled.")

    def add_stream(zf: zipfile.ZipFile, name: str, size: int, write) -> None:
        info = zipfile.ZipInfo(name, date_time=datetime.now(timezone.utc).timetuple()[:6])
        info.compress_type = method
        info.external_attr = 0o644 << 16
        with zf.open(info, "w", force_zip64=size >= 0xFFFFFFFF) as dst:
            sums.append(f"{write(dst)}  {name}")

    try:
        with zipfile.ZipFile(partial, "w", compression=method, allowZip64=True, compresslevel=1) as zf:
            zf.writestr("android-info.txt", f"require board={'|'.join(boards)}\n")
            zf.writestr("super_empty.img", lp.write_image(layout))

            for img in physical:
                def copy(dst, src=img.file, name=img.name):
                    h = hashlib.sha256()
                    with open(src, "rb") as fh:
                        while chunk := fh.read(1 << 20):
                            dst.write(chunk)
                            h.update(chunk)
                            tick(name, len(chunk))
                    return h.hexdigest()
                add_stream(zf, f"{img.name}.img", img.size, copy)

            with open(super_file, "rb") as sf:
                for part in logical:
                    base, _ = lp.strip_slot(part.name)

                    class Counting:
                        def __init__(self, dst, name):
                            self.dst, self.name = dst, name

                        def write(self, data):
                            self.dst.write(data)
                            tick(self.name, len(data))

                    add_stream(zf, f"{base}.img", part.size,
                               lambda dst, p=part, n=base: lp.copy_partition(sf, p, Counting(dst, n)))

            zf.writestr("SHA256SUMS", "\n".join(sums) + "\n")
            zf.writestr("opxflasher-restore.json", json.dumps({
                "tool": "OPxFlasher", "tool_version": __version__,
                "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "from_backup": folder.name, "backup_created_utc": manifest.get("created_utc"),
                "phone": {k: facts.get(k, "") for k in ("model", "codename", "build", "fingerprint",
                                                        "security_patch", "serial", "slot_suffix")},
                "only_for_this_phone": True,
                "physical": [i.name for i in physical],
                "logical": [lp.strip_slot(p.name)[0] for p in logical],
            }, indent=2))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    shutil.move(partial, out)
    return out



def _build_script_zip(folder, out_dir, manifest, facts, physical, super_file, boards, profile,
                      compress, progress, cancel, chunk) -> Path:
    serial, slot = facts.get("serial", ""), facts.get("slot_suffix", "")
    shown = lambda s: re.sub(r"[^A-Za-z0-9._()-]", "_", s or "unknown")  # display only
    for what, value in [("serial number", serial)] + [("board", b) for b in boards]:
        if not _SAFE.match(value or ""):
            raise RestoreError(f"The backup's {what} ({value!r}) can't be checked by the recovery script.")
    big = [i.name for i in physical if i.size >= 0xFFFFFFFF]
    if big:
        raise RestoreError(f"{', '.join(big)} is too large for a recovery zip.")
    firmware = set(profile.qcom_arb_images)
    super_size = super_file.stat().st_size
    total = sum(i.size for i in physical) + super_size
    method = zipfile.ZIP_DEFLATED  # super is mostly empty space: always compressed
    image_method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / zip_name(facts, "orangefox")
    partial = out.with_name(out.name + ".part")
    done = 0
    lines = [f"serial {serial}", f"slot {slot}", f"model {shown(facts.get('model'))}",
             f"build {shown(facts.get('build'))}", f"chunk {chunk}", f"supersize {super_size}"]
    lines += [f"board {b}" for b in boards]
    sums: list[str] = []

    def tick(what: str, n: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress(what, done, total)
        if cancel and cancel():
            raise RestoreError("Cancelled.")

    def entry(zf, name: str, src, length: int, what: str, how) -> str:
        info = zipfile.ZipInfo(name, date_time=datetime.now(timezone.utc).timetuple()[:6])
        info.compress_type = how
        info.external_attr = 0o644 << 16
        h = hashlib.sha256()
        with zf.open(info, "w") as dst:
            left = length
            while left:
                data = src.read(min(1 << 20, left))
                if not data:
                    raise RestoreError(f"{what} is shorter than expected.")
                dst.write(data)
                h.update(data)
                left -= len(data)
                tick(what, len(data))
        sums.append(f"{h.hexdigest()}  {name}")
        return h.hexdigest()

    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True, compresslevel=1) as zf:
            script = zipfile.ZipInfo("META-INF/com/google/android/update-binary")
            script.external_attr = 0o755 << 16
            zf.writestr(script, SCRIPT.read_bytes().replace(b"\r\n", b"\n"), zipfile.ZIP_DEFLATED)
            zf.writestr("META-INF/com/google/android/updater-script", "# OPxFlasher: see update-binary\n")
            for img in physical:
                with open(img.file, "rb") as fh:
                    digest = entry(zf, f"{img.name}.img", fh, img.size, img.name, image_method)
                flag = "firmware" if img.name in firmware else "-"
                lines.append(f"image {img.name} {img.name}.img {digest} {flag}")
            with open(super_file, "rb") as fh:
                for index in range(-(-super_size // chunk)):
                    name = f"super.img.{index:03d}"
                    length = min(chunk, super_size - index * chunk)
                    digest = entry(zf, name, fh, length, f"super part {index + 1}", method)
                    lines += [f"super {index} {name} {digest} -", f"size {name} {length}"]
            zf.writestr("opx/restore.list", "\n".join(lines) + "\n")
            zf.writestr("SHA256SUMS", "\n".join(sums) + "\n")
            zf.writestr("opxflasher-restore.json", json.dumps({
                "tool": "OPxFlasher", "tool_version": __version__, "recovery": "orangefox",
                "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "from_backup": folder.name, "backup_created_utc": manifest.get("created_utc"),
                "phone": {k: facts.get(k, "") for k in ("model", "codename", "build", "fingerprint",
                                                        "security_patch", "serial", "slot_suffix")},
                "only_for_this_phone": True,
                "physical": [i.name for i in physical],
                "firmware_checked": sorted(firmware & {i.name for i in physical}),
            }, indent=2))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    shutil.move(partial, out)
    return out
