"""Phase 1: read-only partition backup with verification.

Nothing in this module writes to the phone. Images are streamed with
``adb exec-out dd if=...``, hashed on the PC while they download, and
optionally checked against ``sha256sum`` run on the phone itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from opx import __version__
from opx.profile import PartitionRule, Profile
from opx.remote import BY_NAME_DIRS, MAPPER_DIR, RemoteShell, check_name

CHUNK = 1024 * 1024

PROP_KEYS = {
    "model": "ro.product.model",
    "codename": "ro.product.device",
    "product": "ro.product.name",
    "build": "ro.build.display.id",
    "fingerprint": "ro.build.fingerprint",
    "security_patch": "ro.build.version.security_patch",
    "android": "ro.build.version.release",
    "slot_suffix": "ro.boot.slot_suffix",
    "verified_boot": "ro.boot.verifiedbootstate",
}

Event = dict
EventFn = Callable[[Event], None]


class Cancelled(Exception):
    pass


class BackupError(RuntimeError):
    pass


@dataclass
class PlanItem:
    name: str
    path: str
    source: str  # "by-name" or "mapper"
    size: int | None = None


@dataclass
class Plan:
    mode: str
    access: str
    profile: str
    device: dict[str, str]
    items: list[PlanItem] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(i.size or 0 for i in self.items)


@dataclass
class ItemResult:
    name: str
    path: str
    source: str
    file: str
    size: int
    sha256: str
    device_sha256: str | None
    status: str  # "ok", "unverified", "failed"
    error: str = ""


def device_facts(props: dict[str, str]) -> dict[str, str]:
    return {k: props.get(v, "") for k, v in PROP_KEYS.items()}


def _partition_tables(shell: RemoteShell) -> tuple[str | None, list[str], list[str]]:
    by_name_dir, physical = None, []
    for d in BY_NAME_DIRS:
        names = shell.list_dir(d)
        if names:
            by_name_dir, physical = d, names
            break
    mapper = [
        n
        for n in shell.list_dir(MAPPER_DIR)
        if n != "control" and not n.endswith(("-cow", "-verity")) and "-cow-" not in n
    ]
    return by_name_dir, physical, mapper


def build_plan(shell: RemoteShell, profile: Profile, mode: str, props: dict[str, str]) -> Plan:
    if not shell.access.usable:
        raise BackupError(
            "Root access is needed to read partitions. Use a rooted system "
            "(Magisk/KernelSU/APatch, and grant Shell root) or a custom recovery."
        )
    plan = Plan(mode=mode, access=shell.access.kind, profile=profile.id, device=device_facts(props))
    for issue in profile.issues_for(props):
        plan.warnings.append(f"[{issue.severity}] {issue.message}")

    by_name_dir, physical, mapper = _partition_tables(shell)
    if not by_name_dir:
        raise BackupError("Could not read the partition list (/dev/block/by-name).")

    def add(name: str, source: str) -> None:
        check_name(name)
        base = by_name_dir if source == "by-name" else MAPPER_DIR
        plan.items.append(PlanItem(name=name, path=f"{base}/{name}", source=source))

    if mode == "full":
        excluded = [PartitionRule(p, False) for p in profile.full_exclude]
        for name in sorted(physical):
            if not any(r.matches(name) for r in excluded):
                add(name, "by-name")
        plan.warnings.append(
            "Full mode reads physical partitions only. 'super' contains every logical "
            "partition (system, my_region, ...), so they are included inside super.img."
        )
    else:
        for rule in profile.rules_for(mode):
            pools = [("mapper", mapper), ("by-name", physical)] if rule.logical else [("by-name", physical)]
            found = False
            for source, pool in pools:
                hits = sorted(n for n in pool if rule.matches(n))
                for n in hits:
                    if not any(i.name == n for i in plan.items):
                        add(n, source)
                if hits:
                    found = True
                    break
            if not found:
                plan.missing.append(rule.name)
        if any(r.logical for r in profile.rules_for(mode)) and shell.access.kind == "root-shell":
            plan.warnings.append(
                "In recovery, logical partitions only appear if the recovery has mapped 'super'."
            )

    for item in plan.items:
        item.size = shell.size_of(item.path)
    return plan


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "unknown"


def backup_folder_name(plan: Plan) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    model = _safe(plan.device.get("model") or "device")
    build = _safe(plan.device.get("build") or "build")
    return f"{model}_{build}_{plan.mode}_{stamp}"


def run_backup(
    shell: RemoteShell,
    plan: Plan,
    out_root: Path,
    verify_on_device: bool = True,
    on_event: EventFn | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    emit = on_event or (lambda e: None)
    cancel = cancel or threading.Event()
    if not plan.items:
        raise BackupError("Nothing to back up for this mode.")

    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    need = int(plan.total_bytes * 1.05)
    free = shutil.disk_usage(out_root).free
    if need > free:
        raise BackupError(
            f"Not enough free space: need about {need / 1e9:.1f} GB, have {free / 1e9:.1f} GB."
        )

    folder = out_root / backup_folder_name(plan)
    folder.mkdir()
    results: list[ItemResult] = []
    started = datetime.now(timezone.utc)
    overall_total = plan.total_bytes
    overall_done = 0

    def write_manifest(finished: bool) -> None:
        manifest = {
            "tool": "OPxFlasher",
            "tool_version": __version__,
            "format": 1,
            "created_utc": started.isoformat(timespec="seconds"),
            "finished": finished,
            "mode": plan.mode,
            "access": plan.access,
            "profile": plan.profile,
            "device": plan.device,
            "missing": plan.missing,
            "warnings": plan.warnings,
            "partitions": [asdict(r) for r in results],
        }
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
        lines = [f"{r.sha256}  {r.file}" for r in results if r.status != "failed"]
        (folder / "SHA256SUMS").write_text("\n".join(lines) + ("\n" if lines else ""), "utf-8")

    emit({"type": "log", "message": f"Backing up {len(plan.items)} partition(s) to {folder}"})
    for index, item in enumerate(plan.items, 1):
        if cancel.is_set():
            raise Cancelled()
        emit({"type": "item_start", "index": index, "total": len(plan.items), "name": item.name, "size": item.size})
        target = folder / f"{item.name}.img"
        partial = folder / f"{item.name}.img.part"
        digest = hashlib.sha256()
        written = 0
        error = ""

        proc = shell.dd_stream(item.path)
        try:
            with open(partial, "wb") as fh:
                assert proc.stdout is not None
                last = 0.0
                while True:
                    if cancel.is_set():
                        proc.kill()
                        raise Cancelled()
                    chunk = proc.stdout.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if now - last > 0.2:
                        last = now
                        emit({
                            "type": "progress",
                            "name": item.name,
                            "done": written,
                            "size": item.size,
                            "overall_done": overall_done + written,
                            "overall_total": overall_total,
                        })
            proc.wait(timeout=60)
            stderr = proc.stderr.read().decode("utf-8", "replace").strip() if proc.stderr else ""
        except Cancelled:
            proc.wait(timeout=10)
            partial.unlink(missing_ok=True)
            write_manifest(False)
            emit({"type": "log", "message": "Backup cancelled. Completed images were kept."})
            raise

        if proc.returncode != 0:
            error = f"adb exited with {proc.returncode}: {stderr}"
        elif written == 0:
            error = "no data read (is root granted to Shell?)"
        elif item.size is not None and written != item.size:
            error = f"size mismatch: read {written} bytes, partition is {item.size}"

        sha = digest.hexdigest()
        device_sha = None
        status = "failed" if error else "unverified"
        if not error:
            partial.replace(target)
            if verify_on_device:
                emit({"type": "log", "message": f"Verifying {item.name} on the phone..."})
                device_sha = shell.sha256_of(item.path)
                if device_sha is None:
                    emit({"type": "log", "message": f"{item.name}: phone has no sha256sum, kept PC hash only."})
                elif device_sha == sha:
                    status = "ok"
                else:
                    status = "failed"
                    error = "hash mismatch between phone and PC copy"
        else:
            partial.unlink(missing_ok=True)

        overall_done += item.size or written
        results.append(
            ItemResult(
                name=item.name,
                path=item.path,
                source=item.source,
                file=target.name,
                size=written,
                sha256=sha,
                device_sha256=device_sha,
                status=status,
                error=error,
            )
        )
        write_manifest(False)
        emit({"type": "item_done", "name": item.name, "status": status, "sha256": sha, "error": error})

    write_manifest(True)
    failed = [r.name for r in results if r.status == "failed"]
    emit({
        "type": "finished",
        "folder": str(folder),
        "ok": sum(r.status == "ok" for r in results),
        "unverified": sum(r.status == "unverified" for r in results),
        "failed": failed,
    })
    return folder


def verify_folder(folder: Path) -> list[tuple[str, bool, str]]:
    """Re-hash a backup folder against its manifest (offline, no phone needed)."""
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
    report = []
    for part in manifest.get("partitions", []):
        if part.get("status") == "failed":
            report.append((part["file"], False, "was not backed up: " + part.get("error", "")))
            continue
        path = folder / part["file"]
        if not path.exists():
            report.append((part["file"], False, "file missing"))
            continue
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while chunk := fh.read(CHUNK):
                h.update(chunk)
        ok = h.hexdigest() == part["sha256"]
        report.append((part["file"], ok, "ok" if ok else "hash does not match manifest"))
    return report
