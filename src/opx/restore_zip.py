"""Build a recovery-flashable restore zip from a Full firmware backup.

The zip uses the standard Android image-zip layout that AERA Recovery installs
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


def zip_name(facts: dict) -> str:
    safe = lambda s: "".join(c if c.isalnum() or c in "._-" else "_" for c in s)
    return (f"OPxFlasher-restore-{safe(facts.get('model', 'phone'))}-{safe(facts.get('build', ''))}"
            f"-{safe(facts.get('serial', ''))}.zip")


def build(folder: Path, out_dir: Path, compress: bool = False,
          progress: Progress | None = None, cancel: Callable[[], bool] | None = None) -> Path:
    """Write the restore zip next to `out_dir`; returns its path."""
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
    total = sum(i.size for i in physical) + sum(p.size for p in logical)
    boards = [codename] + [b for b in profile.recovery_boards if b != codename]

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

