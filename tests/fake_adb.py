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
  FAKE_AERA = 1                            the recovery is a custom one with adb (state "recovery")
  FAKE_AERA_RESULT = <line>                what the zip's installer leaves in /tmp (as if installed on the phone)
  FAKE_PUSH_DROP = 1                       the first copy to /sdcard/OPxFlasher stops half way
  FAKE_PUSH_CORRUPT = 1                    copies to /sdcard/OPxFlasher arrive with one byte changed
  FAKE_FREE_KB = <n>                       free space df reports for /sdcard
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
            user = st.get("user", False)
            if ("-3" in flags and not user) or ("-s" in flags and user):
                continue
            if "-f" in flags:
                path = st.get("path") or (f"/data/app/{name}-1/base.apk" if user
                                          else f"/system/app/{name.rsplit('.', 1)[-1]}/{name}.apk")
                out.write(f"package:{path}={name}\n".encode())
            else:
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


def phone_file(path: str) -> Path:
    """/sdcard/... and /tmp/... on the fake phone."""
    path = path.strip("'\"")
    for prefix in ("/sdcard/", "/tmp/"):
        if path.startswith(prefix):
            return ROOT / prefix.strip("/") / path[len(prefix):]
    raise AssertionError(path)


def aera(args: list[str]) -> int | None:
    """Route B: the copy to internal storage and what the custom recovery answers (None: not one of these)."""
    if args[:1] == ["push"] and args[2].startswith("/sdcard/OPxFlasher/"):
        src, dst = Path(args[1]), phone_file(args[2])
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        log(f"push {args[2]}")
        dropped = ROOT / "push-dropped"
        if os.environ.get("FAKE_PUSH_DROP") and not dropped.exists():
            dropped.write_text("1")
            dst.write_bytes(data[: len(data) // 2])
            sys.stderr.write("adb: error: failed to copy: no devices/emulators found\n")
            return 1
        if os.environ.get("FAKE_PUSH_CORRUPT"):
            data = data[:-1] + bytes([data[-1] ^ 1])
        dst.write_bytes(data)
        return 0
    if args[:1] == ["pull"] and args[1].startswith("/tmp/"):
        f = phone_file(args[1])
        if not f.exists():
            sys.stderr.write(f"adb: error: remote object '{args[1]}' does not exist\n")
            return 1
        Path(args[2]).write_bytes(f.read_bytes())
        return 0
    if args[:1] != ["shell"]:
        return None
    cmd = " ".join(args[1:])
    m = re.fullmatch(r"(stat -c %s|sha256sum|ls|rm -f|mkdir -p|cat) ('?/(?:sdcard|tmp)/\S*)", cmd)
    if cmd == "df -k /sdcard":
        free = int(os.environ.get("FAKE_FREE_KB", str(64 * 1024 * 1024)))
        print("Filesystem 1K-blocks Used Available Use% Mounted on")
        print(f"/dev/fuse 100000000 1 {free} 1% /storage/emulated")
        return 0
    if not m:
        return None
    log(cmd)
    op, f = m.group(1), phone_file(m.group(2))
    if op == "mkdir -p":
        f.mkdir(parents=True, exist_ok=True)
        return 0
    if op == "rm -f":
        if f.exists():
            f.unlink()
        return 0
    if op == "cat" and f.name == "opx-install.result" and not f.exists() and os.environ.get("FAKE_AERA_RESULT"):
        # the owner installed the zip on the phone: the installer left its line, and its log
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(os.environ["FAKE_AERA_RESULT"] + "\n")
        (f.parent / "opx-install.log").write_text("OPxFlasher full ROM installer\n")
    if not f.exists():
        sys.stderr.write(f"{op.split()[0]}: {m.group(2)}: No such file or directory\n")
        return 1
    if op == "stat -c %s":
        print(f.stat().st_size)
    elif op == "sha256sum":
        print(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {m.group(2)}")
    elif op == "ls":
        print(m.group(2).strip("'"))
    else:
        sys.stdout.write(f.read_text())
    return 0


def main(argv: list[str]) -> int:
    args = list(argv)
    if args[:1] == ["-s"]:
        args = args[2:]
    in_aera = boot_mode() == "recovery" and os.environ.get("FAKE_AERA")
    if args[:1] == ["devices"]:
        state = os.environ.get("FAKE_STATE", "device")
        print("List of devices attached")
        if boot_mode() == "android":
            extra = " product:PJZ110 model:PJZ110 device:testdevice transport_id:1" if args[1:2] == ["-l"] else ""
            print(f"FAKE0001       {state}{extra}")
        elif in_aera:
            print("FAKE0001       recovery")
        return 0
    if boot_mode() != "android" and not in_aera:
        sys.stderr.write("adb: no devices/emulators found\n")
        return 1
    handled = aera(args)
    if handled is not None:
        return handled
    if in_aera and args[:1] != ["reboot"]:
        sys.stderr.write(f"fake adb (recovery): unsupported args {args}\n")
        return 99
    if args[:1] == ["push"] and args[2].startswith("/sdcard/debloater/"):
        f = ROOT / "sdcard" / args[2][len("/sdcard/"):]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(Path(args[1]).read_bytes())
        log(f"push {args[2]}")
        return 0
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
        if cmd.startswith("mkdir -p /sdcard/debloater/"):
            return 0
        m = re.fullmatch(r"su -c '(.*)'", cmd)
        if m:
            if ACCESS != "su":
                sys.stderr.write("/system/bin/sh: su: inaccessible or not found\n")
                return 127
            inner = m.group(1)
            # OP Debloater (FAKE_OP_DEBLOAT=1: the module is installed)
            if inner.startswith("command -v debloater"):
                if os.environ.get("FAKE_OP_DEBLOAT"):
                    print("opx-yes")
                return 0
            d = re.fullmatch(r"debloater remove -f (\S+) --label (\S+)", inner)
            if d and os.environ.get("FAKE_OP_DEBLOAT"):
                lst = json.loads((ROOT / "sdcard" / d.group(1)[len("/sdcard/"):]).read_text())["packages"]
                pk = packages()
                for name in lst:
                    if name in pk:
                        pk[name]["removed"] = True
                PACKAGES.write_text(json.dumps(pk))
                log(f"op-debloat remove {' '.join(lst)}")
                print(f"DONE {len(lst)}")
                return 0
            return run(inner, True)
        return run(cmd, root)
    sys.stderr.write(f"fake adb: unsupported args {args}\n")
    return 99


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
