"""A tiny stand-in for adb used by the tests.

It serves partitions from a folder instead of a real phone:
  $FAKE_DEVICE_DIR/by-name/<partition>   physical partitions
  $FAKE_DEVICE_DIR/mapper/<partition>    logical partitions
  $FAKE_DEVICE_DIR/props.json            getprop values
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


def run(cmd: str, root: bool) -> int:
    out = sys.stdout.buffer
    if cmd == "id -u":
        out.write(b"0\n" if root else b"2000\n")
        return 0
    if cmd == "getprop":
        props = json.loads((ROOT / "props.json").read_text())
        for k, v in props.items():
            out.write(f"[{k}]: [{v}]\n".encode())
        return 0
    if not root:
        sys.stderr.write("Permission denied\n")
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


def main(argv: list[str]) -> int:
    args = list(argv)
    if args[:1] == ["-s"]:
        args = args[2:]
    if args[:2] == ["devices", "-l"]:
        state = os.environ.get("FAKE_STATE", "device")
        print("List of devices attached")
        print(f"FAKE0001       {state} product:PJZ110 model:PJZ110 device:testdevice transport_id:1")
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
