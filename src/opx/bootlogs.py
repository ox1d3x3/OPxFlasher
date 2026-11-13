"""Save the phone's logs for a bug report or a boot problem.

Collects: Android log (logcat, all buffers), kernel log (dmesg, needs root),
the log of the last crash / reboot (/sys/fs/pstore, /proc/last_kmsg, needs
root) and the phone's properties. Serial numbers are hidden. Read-only.
"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

from opx import bootdiag
from opx.support import mask


def collect(session, out_dir: Path, emit=None) -> Path:
    say = emit or (lambda text: None)
    shell, serial = session.shell, session.serial
    serials = [serial, session.props.get("ro.serialno", "")]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"OPxFlasher-logs-{stamp}.zip"
    notes = []
    saved: dict[str, str] = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as raw_zf:
        class _Zip:  # remembers what was written, for the findings
            def writestr(self, name, text):
                saved[name] = text
                raw_zf.writestr(name, text)
        zf = _Zip()
        say("Phone properties")
        zf.writestr("getprop.txt", mask("".join(f"[{k}]: [{v}]\n" for k, v in sorted(session.props.items())), serials))

        say("Android log (logcat)")
        res = shell.read_log("logcat")
        zf.writestr("logcat.txt", mask(res.stdout, serials))
        if not res.ok:
            notes.append(f"logcat: {res.stderr.strip()}")

        if not shell.access.usable:
            notes.append("No root: the kernel log and last-crash logs need root, so they're not included.")
        else:
            say("Kernel log (dmesg)")
            res = shell.read_log("dmesg")
            zf.writestr("dmesg.txt", mask(res.stdout, serials))
            if not res.ok:
                notes.append(f"dmesg: {res.stderr.strip()}")
            say("Last crash / reboot")
            found = 0
            for name in shell.list_pstore():
                res = shell.read_log("pstore", name)
                if res.ok and res.stdout:
                    zf.writestr(f"pstore/{name}", mask(res.stdout, serials))
                    found += 1
            res = shell.read_log("last_kmsg")
            if res.ok and res.stdout:
                zf.writestr("last_kmsg.txt", mask(res.stdout, serials))
                found += 1
            if not found:
                notes.append("No last-crash log on the phone (it may not have crashed since it last started).")
        say("Looking for known problems")
        zf.writestr("findings.txt", bootdiag.report(bootdiag.analyze(saved)))
        zf.writestr("README.txt", "Logs saved by OPxFlasher. Serial numbers are hidden.\n"
                    "findings.txt lists known problem messages found in these logs (hints, not a diagnosis).\n\n"
                    + "\n".join(notes) + "\n")
    return out
