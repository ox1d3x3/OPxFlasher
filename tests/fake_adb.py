"""A tiny stand-in for adb used by the tests.

It serves partitions from a folder instead of a real phone:
  $FAKE_DEVICE_DIR/by-name/<partition>   physical partitions
  $FAKE_DEVICE_DIR/mapper/<partition>    logical partitions
  $FAKE_DEVICE_DIR/props.json            getprop values
  $FAKE_DEVICE_DIR/packages.json         system apps {name: {"removed": bool, "disabled": bool}}
  $FAKE_DEVICE_DIR/pstore/<file>         last-crash logs
Environment knobs:
  FAKE_ACCESS = su | root-shell | none   how root is obtained
  FAKE_NO_SHA = 1                         phone has no sha256sum
  FAKE_CORRUPT = <partition>              flip a byte in dd output
  FAKE_STATE = device | recovery | unauthorized
  FAKE_LOG = <file>                        append every command run
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ["FAKE_DEVICE_DIR"])
ACCESS = os.environ.get("FAKE_ACCESS", "su")


def log(cmd: str) -> None:
    path = os.environ.get("FAKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(cmd + "\n")


def local(path: str) -> Path | None:
    for prefix, sub in (("/dev/block/by-name/", "by-name"), ("/dev/block/mapper/", "mapper")):
        if path.startswith(prefix):
            return ROOT / sub / path[len(prefix):]
    if path == "/dev/block/by-name":
        return ROOT / "by-name"
    if path == "/dev/block/mapper":
        return ROOT / "mapper"
    return None


DOWNLOAD = ROOT / "sdcard" / "Download"
PACKAGES = ROOT / "packages.json"
DEFAULT_PACKAGES = ["android", "com.android.systemui", "com.android.settings", "com.android.providers.media",
                    "com.heytap.browser", "com.oplus.games", "com.coloros.weather2", "com.android.chrome"]


def packages() -> dict:
    if PACKAGES.exists():
        return json.loads(PACKAGES.read_text())
    return {p: {"removed": False, "disabled": p == "com.coloros.weather2"} for p in DEFAULT_PACKAGES}


def run_unprivileged(cmd: str, out) -> int | None:
    """Commands that work without root (None = not one of them)."""
    if cmd.startswith("pm list packages"):
        flags = cmd.split()[3:]
        for name, st in sorted(packages().items()):
            if st["removed"] and "-u" not in flags:
                continue
            if "-d" in flags and not st["disabled"]:
                continue
            out.write(f"package:{name}\n".encode())
        return 0
    m = re.fullmatch(r"pm uninstall -k --user 0 (\S+)", cmd)
    if m:
        pk = packages()
        st = pk.get(m.group(1))
        if not st or st["removed"]:
            out.write(b"Failure [not installed for 0]\n")
            return 1
        st["removed"] = True
        PACKAGES.write_text(json.dumps(pk))
        out.write(b"Success\n")
        return 0
    m = re.fullmatch(r"cmd package install-existing --user 0 (\S+)", cmd)
    if m:
        pk = packages()
        if m.group(1) not in pk:
            sys.stderr.write(f"Package {m.group(1)} doesn't exist\n")
            return 1
        pk[m.group(1)]["removed"] = False
        PACKAGES.write_text(json.dumps(pk))
        out.write(f"Package {m.group(1)} installed for user: 0\n".encode())
        return 0
    if cmd == "logcat -d -b all":
        out.write(b"11-13 08:00:00.000  1000  1000 I ActivityManager: Start proc FAKE0001\n")
        return 0
    return None


def run(cmd: str, root: bool) -> int:
    out = sys.stdout.buffer
    if cmd == "ls -t /sdcard/Download":  # shared storage needs no root
        DOWNLOAD.mkdir(parents=True, exist_ok=True)
        files = sorted(DOWNLOAD.iterdir(), key=lambda f: f.stat().st_mtime_ns, reverse=True)
        out.write("".join(f.name + "\n" for f in files).encode())
        return 0
    if cmd == "id -u":
        out.write(b"0\n" if root else b"2000\n")
        return 0
    handled = run_unprivileged(cmd, out)
    if handled is not None:
        return handled
    if cmd == "getprop":
        props = json.loads((ROOT / "props.json").read_text())
        for k, v in props.items():
            out.write(f"[{k}]: [{v}]\n".encode())
        return 0
    if not root:
        sys.stderr.write("Permission denied\n")
        return 1
    if cmd == "dmesg":
        out.write(b"[    0.000000] Booting Linux on physical CPU 0x0 serial FAKE0001\n")
        return 0
    if cmd == "ls -1 /sys/fs/pstore":
        d = ROOT / "pstore"
        for name in sorted(os.listdir(d)) if d.is_dir() else []:
            out.write((name + "\n").encode())
        return 0
    m = re.fullmatch(r"cat /sys/fs/pstore/(\S+)", cmd)
    if m:
        f = ROOT / "pstore" / m.group(1)
        if not f.exists():
            sys.stderr.write("No such file or directory\n")
            return 1
        out.write(f.read_bytes())
        return 0
    if cmd == "cat /proc/last_kmsg":
        sys.stderr.write("cat: /proc/last_kmsg: No such file or directory\n")
        return 1
    m = re.fullmatch(r"ls -1 (\S+)", cmd)
    if m:
        p = local(m.group(1))
        if not p or not p.is_dir():
            sys.stderr.write("No such file or directory\n")
            return 1
        for name in sorted(os.listdir(p)):
            out.write((name + "\n").encode())
        return 0
    m = re.fullmatch(r"blockdev --getsize64 (\S+)", cmd)
    if m:
        p = local(m.group(1))
        out.write(f"{p.stat().st_size}\n".encode())
        return 0
    m = re.fullmatch(r"sha256sum (\S+)", cmd)
    if m:
        if os.environ.get("FAKE_NO_SHA"):
            sys.stderr.write("sha256sum: inaccessible or not found\n")
            return 127
        p = local(m.group(1))
        out.write(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {m.group(1)}\n".encode())
        return 0
    m = re.fullmatch(r"dd if=(\S+) bs=\d+ 2>/dev/null", cmd)
    if m:
        p = local(m.group(1))
        data = bytearray(p.read_bytes())
        if os.environ.get("FAKE_CORRUPT") == p.name and data:
            data[0] ^= 0xFF
        out.write(bytes(data))
        return 0
    sys.stderr.write(f"fake adb: unsupported command: {cmd}\n")
    return 99


def boot_mode() -> str:
    """Mode shared with fake_fastboot.py (android when no state file)."""
    state = ROOT / "fastboot.json"
    return json.loads(state.read_text())["mode"] if state.exists() else "android"


def main(argv: list[str]) -> int:
    args = list(argv)
    if args[:1] == ["-s"]:
        args = args[2:]
    if args[:1] == ["devices"]:
        state = os.environ.get("FAKE_STATE", "device")
        print("List of devices attached")
        if boot_mode() == "android":
            extra = " product:PJZ110 model:PJZ110 device:testdevice transport_id:1" if args[1:2] == ["-l"] else ""
            print(f"FAKE0001       {state}{extra}")
        return 0
    if boot_mode() != "android":
        sys.stderr.write("adb: no devices/emulators found\n")
        return 1
    if args[:1] == ["push"]:
        src, dst = Path(args[1]), args[2]
        assert dst.startswith("/sdcard/Download/"), dst
        DOWNLOAD.mkdir(parents=True, exist_ok=True)
        (DOWNLOAD / dst.rsplit("/", 1)[1]).write_bytes(src.read_bytes())
        log(f"push {dst}")
        return 0
    if args[:1] == ["pull"]:
        src, dst = args[1], Path(args[2])
        f = DOWNLOAD / src.rsplit("/", 1)[1]
        if not f.exists():
            sys.stderr.write(f"adb: error: remote object '{src}' does not exist\n")
            return 1
        dst.write_bytes(f.read_bytes())
        return 0
    if args[:1] == ["reboot"]:
        state_file = ROOT / "fastboot.json"
        state = json.loads(state_file.read_text())
        target = args[1] if len(args) > 1 else ""
        state["mode"] = {"bootloader": "bootloader", "fastboot": "fastbootd", "recovery": "recovery", "": "android"}[target]
        state.setdefault("log", []).append(f"adb reboot {target}".strip())
        state_file.write_text(json.dumps(state, indent=1))
        return 0
    if args and args[0] in ("shell", "exec-out"):
        cmd = " ".join(args[1:])
        log(cmd)
        root = ACCESS == "root-shell"
        m = re.fullmatch(r"su -c '(.*)'", cmd)
        if m:
            if ACCESS != "su":
                sys.stderr.write("/system/bin/sh: su: inaccessible or not found\n")
                return 127
            return run(m.group(1), True)
        return run(cmd, root)
    sys.stderr.write(f"fake adb: unsupported args {args}\n")
    return 99


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
