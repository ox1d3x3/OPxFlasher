"""Partition explorer: every partition on the phone with its size and role.
Read-only (needs root, like backups)."""

from __future__ import annotations

from dataclasses import dataclass

from opx.backup import _partition_tables
from opx.profile import Profile, is_user_data
from opx.remote import MAPPER_DIR, RemoteShell


class ExplorerError(RuntimeError):
    pass


@dataclass
class PartitionRow:
    name: str
    size: int | None
    kind: str   # "physical" or "logical"
    slot: str   # "a", "b" or ""
    role: str


def role_of(name: str, kind: str, slot: str, running: str, profile: Profile) -> str:
    if is_user_data(name):
        return "Your data (never backed up or flashed)"
    if profile.is_never_flash(name):
        return "Phone identity / calibration (never flashed)"
    where = "inside super" if kind == "logical" else ""
    if not slot:
        return ("Shared, " + where) if where else "Shared (no slot)"
    text = "Running slot" if slot == running else "Other slot"
    return f"{text}, {where}" if where else text


def explore(shell: RemoteShell, profile: Profile, props: dict[str, str]) -> list[PartitionRow]:
    if not shell.access.usable:
        raise ExplorerError("Listing partitions needs root (Magisk / KernelSU / APatch) or a recovery.")
    by_name, physical, logical, _ = _partition_tables(shell)
    if not by_name:
        raise ExplorerError("Could not read the partition list (/dev/block/by-name).")
    running = props.get("ro.boot.slot_suffix", "").lstrip("_")
    rows = []
    for kind, base, names in (("physical", by_name, physical), ("logical", MAPPER_DIR, logical)):
        for name in names:
            slot = name[-1] if name.endswith(("_a", "_b")) else ""
            rows.append(PartitionRow(name, shell.size_of(f"{base}/{name}"), kind, slot,
                                     role_of(name, kind, slot, running, profile)))
    return rows


def to_csv(rows: list[PartitionRow]) -> str:
    lines = ["name,size_bytes,kind,slot,role"]
    for r in rows:
        lines.append(f"{r.name},{'' if r.size is None else r.size},{r.kind},{r.slot},\"{r.role}\"")
    return "\n".join(lines) + "\n"
