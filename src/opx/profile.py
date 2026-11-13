"""Device profiles: which partitions matter for a given phone.

Profiles are JSON files in ``opx/profiles``. A profile can inherit from
another; the generic OPlus profile is the fallback when nothing matches.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
import re
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
    recovery_boards: list[str] = field(default_factory=list)  # names custom recoveries report (restore zip)
    keep_apps: list[str] = field(default_factory=list)  # packages debloat never removes ("pkg" or "prefix*")
    rollback_fuse: str = ""  # OS version (e.g. "16.0.3.501") from which installing an older build blows a fuse
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


GENERIC = "oplus-generic"
ISSUE_SEVERITIES = ("info", "warning", "danger")
BLOCKABLE = ("fastbootd", "bootloader")
KNOWN_KEYS = {"id", "name", "inherits", "match", "notes", "backup", "never_flash", "known_issues",
              "conditional", "avb_images", "variants", "recovery_boards", "qcom_arb_images", "keep_apps", "rollback_fuse",
              "qcom_arb_validated"}


class ProfileError(ValueError):
    pass


def user_profiles_dir() -> Path:
    """Profiles people add themselves (to try a new phone before it's in a release)."""
    from opx.tools import user_tools_dir

    return user_tools_dir().parent / "profiles"


def _builtin_ids() -> set[str]:
    folder = resources.files("opx.profiles")
    return {p.name[:-5] for p in folder.iterdir() if p.name.endswith(".json")}


def _read(profile_id: str) -> dict:
    if profile_id in _builtin_ids():  # built-in profiles can't be replaced by user files
        data = resources.files("opx.profiles").joinpath(f"{profile_id}.json").read_text("utf-8")
    else:
        f = user_profiles_dir() / f"{profile_id}.json"
        if not f.is_file():
            raise ProfileError(f"No profile called {profile_id!r}.")
        data = f.read_text("utf-8")
    return json.loads(data)


def _merge(base: dict, raw: dict) -> dict:
    merged = {**base, **raw}
    merged["backup"] = {**base.get("backup", {}), **raw.get("backup", {})}
    merged["known_issues"] = base.get("known_issues", []) + raw.get("known_issues", [])
    # A child can add never-flash partitions, never remove the parent's.
    merged["never_flash"] = list(dict.fromkeys(base.get("never_flash", []) + raw.get("never_flash", [])))
    merged["keep_apps"] = list(dict.fromkeys(base.get("keep_apps", []) + raw.get("keep_apps", [])))
    return merged


def _load_raw(profile_id: str) -> dict:
    raw = _read(profile_id)
    parent = raw.get("inherits")
    if parent:
        return _merge(_load_raw(parent), raw)
    return raw


def validate(raw: dict, builtin: bool = False) -> list[str]:
    """Problems with a profile's JSON (empty list = fine)."""
    errors = []
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        errors.append(f"unknown keys: {', '.join(sorted(unknown))}")
    pid = raw.get("id")
    if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", pid or ""):
        errors.append("id must be lower-case letters, digits and dashes (e.g. 'oneplus-12')")
    if not isinstance(raw.get("name"), str) or not raw.get("name"):
        errors.append("name is required (e.g. 'OnePlus 12')")
    if pid != GENERIC:
        if raw.get("inherits") != GENERIC:
            errors.append(f"device profiles must inherit from '{GENERIC}'")
        match = raw.get("match")
        if not isinstance(match, dict) or not match or not all(
                isinstance(k, str) and k.startswith("ro.") and isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)
                for k, v in match.items()):
            errors.append("match must map ro.* properties to non-empty lists, e.g. "
                          '{"ro.product.model": ["CPH2581"]}')
    for name in raw.get("never_flash", []) + raw.get("conditional", []) + raw.get("avb_images", []) \
            + raw.get("qcom_arb_images", []):
        if not re.fullmatch(r"[A-Za-z0-9_.*-]{1,64}", str(name)):
            errors.append(f"bad partition name {name!r}")
    variants = raw.get("variants", {})
    if not isinstance(variants, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in variants.items()):
        errors.append("variants must map model numbers to region names")
    boards = raw.get("recovery_boards", [])
    if not isinstance(boards, list) or not all(isinstance(b, str) and b for b in boards):
        errors.append("recovery_boards must be a list of names")
    fuse = raw.get("rollback_fuse", "")
    if fuse and not (isinstance(fuse, str) and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", fuse)):
        errors.append('rollback_fuse must be an OS version like "16.0.3.501"')
    keep = raw.get("keep_apps", [])
    if not isinstance(keep, list) or not all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.]+\*?", k) for k in keep):
        errors.append("keep_apps must be a list of package names (a trailing * matches a prefix)")
    for i, issue in enumerate(raw.get("known_issues", [])):
        if not isinstance(issue, dict) or not isinstance(issue.get("match"), dict) or not issue.get("message"):
            errors.append(f"known_issues[{i}] needs match and message")
            continue
        if issue.get("severity", "warning") not in ISSUE_SEVERITIES:
            errors.append(f"known_issues[{i}].severity must be one of {', '.join(ISSUE_SEVERITIES)}")
        if not set(issue.get("blocks", [])) <= set(BLOCKABLE):
            errors.append(f"known_issues[{i}].blocks may only contain {', '.join(BLOCKABLE)}")
    if raw.get("qcom_arb_validated") and not builtin:
        errors.append("qcom_arb_validated can only be set in a built-in profile, after checking real firmware")
    return errors


def check_file(path: Path) -> list[str]:
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as e:
        return [f"can't read it as JSON: {e}"]
    if not isinstance(raw, dict):
        return ["the file must contain one JSON object"]
    errors = validate(raw)
    if not errors:
        try:
            load_raw_profile(raw)
        except (ProfileError, TypeError, KeyError) as e:
            errors.append(str(e))
    return errors


def load_raw_profile(raw: dict) -> "Profile":
    """Build a Profile from a dict (with inheritance), for checking a file before installing it."""
    parent = raw.get("inherits")
    return _build(_merge(_load_raw(parent), raw) if parent else raw)


def load(profile_id: str) -> Profile:
    return _build(_load_raw(profile_id))


def _build(raw: dict) -> Profile:
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
        recovery_boards=raw.get("recovery_boards", []),
        keep_apps=raw.get("keep_apps", []),
        rollback_fuse=raw.get("rollback_fuse", ""),
        qcom_arb_images=raw.get("qcom_arb_images", []),
        qcom_arb_validated=bool(raw.get("qcom_arb_validated", False)),
    )


def all_profile_ids() -> list[str]:
    """Built-in profiles, then valid user profiles (user files never replace built-ins)."""
    ids = sorted(_builtin_ids())
    folder = user_profiles_dir()
    if folder.is_dir():
        for f in sorted(folder.glob("*.json")):
            if f.stem in ids:
                continue
            try:
                raw = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict) and raw.get("id") == f.stem and not validate(raw):
                ids.append(f.stem)
    return ids


def detect(props: dict[str, str]) -> Profile:
    for pid in all_profile_ids():
        if pid == "oplus-generic":
            continue
        prof = load(pid)
        if prof.match and all(props.get(k, "") in v for k, v in prof.match.items()):
            return prof
    return load("oplus-generic")
