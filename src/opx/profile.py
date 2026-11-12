"""Device profiles: which partitions matter for a given phone.

Profiles are JSON files in ``opx/profiles``. A profile can inherit from
another; the generic OPlus profile is the fallback when nothing matches.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from importlib import resources

MODES = ("network", "critical", "full")

# Partitions that hold the user's own data rather than the OS: apps, photos and
# files (userdata), the encryption data for them (metadata) and the Google
# account lock (frp). They are never backed up and never flashed, whatever a
# device profile says. A factory reset ("fastboot -w") still erases userdata
# and metadata; that is a wipe, not a flash.
USER_DATA_PARTITIONS = ("userdata", "metadata", "frp")


def is_user_data(partition: str) -> bool:
    return any(PartitionRule(p, False).matches(partition) for p in USER_DATA_PARTITIONS)

MODE_TEXT = {
    "network": "Network essentials (oplusstanvbk + my_region)",
    "critical": "Critical (network + persist, modem, ocdt, oplusreserve ...)",
    "full": "Full firmware (the whole OS, none of your personal data)",
}


@dataclass
class PartitionRule:
    name: str  # may contain * wildcards
    logical: bool

    def matches(self, candidate: str) -> bool:
        for slot in ("", "_a", "_b"):
            if fnmatch.fnmatchcase(candidate, self.name + slot):
                return True
        return False


@dataclass
class KnownIssue:
    match: dict[str, str]
    severity: str
    message: str
    blocks: list[str] = field(default_factory=list)  # e.g. ["fastbootd", "bootloader"]

    def applies(self, props: dict[str, str]) -> bool:
        return all(fnmatch.fnmatchcase(props.get(k, ""), v) for k, v in self.match.items())


@dataclass
class Profile:
    id: str
    name: str
    match: dict[str, list[str]]
    notes: str
    network: list[PartitionRule]
    critical: list[PartitionRule]
    full_exclude: list[str]
    never_flash: list[str]
    known_issues: list[KnownIssue] = field(default_factory=list)
    conditional: list[str] = field(default_factory=list)
    avb_images: list[str] = field(default_factory=list)
    variants: dict[str, str] = field(default_factory=dict)  # model number -> region name
    qcom_arb_images: list[str] = field(default_factory=list)
    qcom_arb_validated: bool = False

    def rules_for(self, mode: str) -> list[PartitionRule]:
        if mode == "network":
            return list(self.network)
        if mode == "critical":
            return list(self.network) + list(self.critical)
        raise ValueError(f"mode {mode!r} has no rule list (full mode uses the partition table)")

    def is_never_flash(self, partition: str) -> bool:
        if is_user_data(partition):
            return True
        return any(PartitionRule(p, False).matches(partition) for p in self.never_flash)

    def variant_name(self, model: str) -> str:
        """e.g. "OnePlus 13 China (PJZ110)"; falls back to the model number."""
        region = self.variants.get(model.upper())
        return f"{self.name} {region} ({model})" if region else model

    def is_conditional(self, partition: str) -> bool:
        return any(PartitionRule(p, False).matches(partition) for p in self.conditional)

    def issues_for(self, props: dict[str, str]) -> list[KnownIssue]:
        return [i for i in self.known_issues if i.applies(props)]

    def blocked_actions(self, props: dict[str, str]) -> set[str]:
        return {b for i in self.issues_for(props) for b in i.blocks}


def _load_raw(profile_id: str) -> dict:
    data = resources.files("opx.profiles").joinpath(f"{profile_id}.json").read_text("utf-8")
    raw = json.loads(data)
    parent = raw.get("inherits")
    if parent:
        base = _load_raw(parent)
        base_backup = base.get("backup", {})
        merged = {**base, **raw}
        merged["backup"] = {**base_backup, **raw.get("backup", {})}
        merged["known_issues"] = base.get("known_issues", []) + raw.get("known_issues", [])
        return merged
    return raw


def load(profile_id: str) -> Profile:
    raw = _load_raw(profile_id)
    backup = raw.get("backup", {})
    return Profile(
        id=raw["id"],
        name=raw["name"],
        match=raw.get("match", {}),
        notes=raw.get("notes", ""),
        network=[PartitionRule(**r) for r in backup.get("network", [])],
        critical=[PartitionRule(**r) for r in backup.get("critical", [])],
        full_exclude=backup.get("full_exclude", ["userdata"]),
        never_flash=raw.get("never_flash", []),
        known_issues=[KnownIssue(**i) for i in raw.get("known_issues", [])],
        conditional=raw.get("conditional", []),
        avb_images=raw.get("avb_images", ["vbmeta"]),
        variants=raw.get("variants", {}),
        qcom_arb_images=raw.get("qcom_arb_images", []),
        qcom_arb_validated=bool(raw.get("qcom_arb_validated", False)),
    )


def all_profile_ids() -> list[str]:
    folder = resources.files("opx.profiles")
    return sorted(p.name[:-5] for p in folder.iterdir() if p.name.endswith(".json"))


def detect(props: dict[str, str]) -> Profile:
    for pid in all_profile_ids():
        if pid == "oplus-generic":
            continue
        prof = load(pid)
        if prof.match and all(props.get(k, "") in v for k, v in prof.match.items()):
            return prof
    return load("oplus-generic")
