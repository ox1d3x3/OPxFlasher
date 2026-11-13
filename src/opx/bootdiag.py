"""Read saved phone logs and point out common reasons a phone won't start.

Works on the zip that "Save phone logs" / ``opx logs`` writes (or any folder of
log files). It only looks for well-known messages from Android, the kernel and
verified boot, so its answers are hints ("looks like"), not a diagnosis.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

# (id, severity, title, what it usually means, patterns)
RULES = [
    ("panic", "problem", "The kernel crashed (kernel panic)",
     "The phone's kernel stopped with a fatal error. After flashing, this usually means a boot, init_boot, "
     "vendor_boot or dtbo image that doesn't match the rest of the build (or a custom kernel that doesn't fit).",
     [r"Kernel panic - not syncing", r"\bkernel BUG at\b"]),
    ("verity", "problem", "Verified boot found a modified partition",
     "dm-verity / AVB rejected a partition: its contents don't match the build's signatures. Typical after "
     "flashing an image from another build, or a vbmeta without verification turned off.",
     [r"dm[-_]verity.*(corrupt|error|fail)", r"avb_slot_verify.*(ERROR|FAIL)", r"[Vv]erification failed",
      r"AVB.*rollback index"]),
    ("first-stage-mount", "problem", "System partitions couldn't be mounted",
     "First-stage init couldn't find or mount system / vendor / product. Usually super's layout or a logical "
     "partition is missing or from another build, or the slot was switched without flashing it.",
     [r"[Ff]ailed to mount (required partitions|/system|/vendor|/product)", r"first.stage.*[Ff]ailed",
      r"fs_mgr.*[Cc]ould not find .* in super", r"Failed to create logical partition"]),
    ("data", "problem", "Your data partition couldn't be opened",
     "Android couldn't mount or unlock /data. After going to another build or region this normally needs a data "
     "wipe (factory reset); with the same build it can be an encryption or keystore problem.",
     [r"[Ff]ailed to mount /data", r"fs_mgr_mount_all.*(fail|error)", r"[Ff]ailed to (decrypt|unlock) (user|/data)",
      r"vold.*(FBE|encryption).*(fail|error)", r"Keymaster.*(fail|error)|KeyMint.*(fail|error)"]),
    ("zygote", "problem", "Android's app runtime keeps crashing (zygote)",
     "zygote, which starts every app, keeps dying. Common causes: a root / Xposed / LSPosed module, or system "
     "and framework files from different builds. Try booting with modules disabled.",
     [r"Service 'zygote\d*' .*(received signal|exited with status)", r"[Zz]ygote.*died",
      r"Process zygote\d* .*exited due to signal"]),
    ("system-server", "problem", "The system process crashed",
     "system_server (Android's core) hit a fatal error. Often a root module, a removed system app it depends on, "
     "or mismatched system files.",
     [r"FATAL EXCEPTION IN SYSTEM PROCESS", r"\*\*\* WATCHDOG KILLING SYSTEM PROCESS"]),
    ("ota-merge", "warning", "A system update didn't finish",
     "Virtual A/B update merge errors (snapuserd / update_engine). Let Android finish the update, or flash the "
     "full build again.",
     [r"snapuserd.*(error|fail)", r"[Mm]erge (failed|error)", r"update_engine.*(ERROR|error code)"]),
    ("modem", "warning", "The modem crashed",
     "The mobile network chip restarted with an error. After a region change this usually means the region / "
     "network files (oplusstanvbk, my_region) don't match the phone; restore your own from a backup.",
     [r"subsys.*modem.*(crash|fatal|restart)", r"modem.*(crashed|SSR|fatal error)", r"mss.*fatal"]),
    ("removed-app", "warning", "A removed system app is still needed",
     "Something tried to use a system app that isn't installed for this user. If you used the Apps tab, "
     "restore what you removed.",
     [r"Unable to find (app|package).*for (intent|component)", r"package .* is not installed for user 0"]),
]

BOOT_REASONS = {
    "kernel_panic": "kernel panic", "watchdog": "watchdog (the phone froze)", "reboot,bootloader": "to bootloader",
    "reboot,recovery": "to recovery", "shutdown,thermal": "overheating", "reboot,rescueparty": "Rescue Party "
    "(Android gave up after repeated crashes)", "bootloader": "to bootloader",
}


@dataclass
class Finding:
    id: str
    severity: str  # "problem", "warning", "info"
    title: str
    meaning: str
    evidence: str = ""
    source: str = ""
    count: int = 0


def read_logs(path: Path) -> dict[str, str]:
    """name -> text, from a logs zip or a folder."""
    path = Path(path)
    out: dict[str, str] = {}
    if path.is_dir():
        for f in sorted(path.rglob("*")):
            if f.is_file() and f.stat().st_size < 64 << 20:
                out[str(f.relative_to(path))] = f.read_text("utf-8", "replace")
        return out
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if not info.is_dir() and info.file_size < 64 << 20:
                out[info.filename] = zf.read(info).decode("utf-8", "replace")
    return out


def analyze(logs: dict[str, str]) -> list[Finding]:
    found: list[Finding] = []
    for rule_id, severity, title, meaning, patterns in RULES:
        regex = re.compile("|".join(f"(?:{p})" for p in patterns))
        hit = None
        count = 0
        for name, text in logs.items():
            if name.lower() in ("readme.txt", "findings.txt"):
                continue
            for line in text.splitlines():
                if regex.search(line):
                    count += 1
                    if hit is None:
                        hit = (line.strip()[:300], name)
        if hit:
            found.append(Finding(rule_id, severity, title, meaning, hit[0], hit[1], count))
    props = logs.get("getprop.txt", "")
    for key in ("sys.boot.reason", "ro.boot.bootreason", "persist.sys.boot.reason"):
        m = re.search(rf"^\[{re.escape(key)}\]: \[(.*)\]$", props, re.M)
        if m and m.group(1):
            reason = m.group(1)
            text = BOOT_REASONS.get(reason, reason)
            severity = "warning" if reason in ("kernel_panic", "watchdog", "reboot,rescueparty") else "info"
            found.append(Finding("boot-reason", severity, f"Last restart reason: {text}",
                                 "What the phone recorded as the reason for its last restart.",
                                 f"[{key}]: [{reason}]", "getprop.txt", 1))
            break
    order = {"problem": 0, "warning": 1, "info": 2}
    return sorted(found, key=lambda f: order[f.severity])


def report(findings: list[Finding]) -> str:
    if not findings:
        return ("No known problem messages found in these logs. That doesn't mean everything is fine: "
                "attach the logs zip to a bug report.\n")
    lines = []
    for f in findings:
        mark = {"problem": "!!", "warning": "! ", "info": "- "}[f.severity]
        lines.append(f"{mark} {f.title}")
        lines.append(f"   {f.meaning}")
        if f.evidence:
            more = f" (+{f.count - 1} more)" if f.count > 1 else ""
            lines.append(f"   Seen in {f.source}{more}: {f.evidence}")
        lines.append("")
    return "\n".join(lines)
