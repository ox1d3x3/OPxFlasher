"""A stand-in for fastboot used by the tests, backed by the same fake phone
folder as fake_adb.py plus a state file:

  $FAKE_DEVICE_DIR/fastboot.json
    mode              android | bootloader | fastbootd | brick
    serial, slot, unlocked, super_size
    fastbootd_broken  rebooting to fastbootd bricks the phone (ColorOS 17 CN)
    fail_flash        partition name whose flash fails
    log               every command that changed something
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ["FAKE_DEVICE_DIR"])
STATE = ROOT / "fastboot.json"


def load() -> dict:
    return json.loads(STATE.read_text())


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=1))


def err(text: str) -> None:
    sys.stderr.write(text + "\n")


def physical() -> dict:
    return {p.name: p.stat().st_size for p in (ROOT / "by-name").iterdir()}


def logical() -> dict:
    return {p.name: p.stat().st_size for p in (ROOT / "mapper").iterdir() if p.name != "control"}


def main(argv: list[str]) -> int:
    state = load()
    args = list(argv)
    if args[:1] == ["-s"]:
        args = args[2:]
    mode = state["mode"]
    in_fastboot = mode in ("bootloader", "fastbootd")

    if args == ["devices"]:
        if in_fastboot:
            print(f"{state['serial']}\tfastboot")
        return 0
    if not in_fastboot:
        err("< waiting for any device >")
        return 1

    slot = state["slot"]
    phys, logi = physical(), logical()

    def var(name: str) -> str | None:
        fixed = {
            "product": "testdevice",
            "unlocked": "yes" if state["unlocked"] else "no",
            "current-slot": slot,
            "slot-count": "2",
            "secure": "yes",
            "is-userspace": "yes" if mode == "fastbootd" else "no",
            "max-download-size": "0x10000000",
            "snapshot-update-status": state.get("snapshot", "none"),
        }
        if name in fixed:
            return fixed[name]
        if name.startswith("has-slot:"):
            p = name.split(":", 1)[1]
            return "yes" if f"{p}_a" in phys or f"{p}_a" in logi else "no"
        if name.startswith("partition-size:"):
            p = name.split(":", 1)[1]
            if p == "super":
                return hex(state["super_size"])
            if p in phys:
                return hex(phys[p])
            if mode == "fastbootd" and p in logi:
                return hex(logi[p])
            return None
        if name.startswith("is-logical:"):
            p = name.split(":", 1)[1]
            return "yes" if p in logi else ("no" if p in phys or p == "super" else None)
        return None

    if args[:1] == ["getvar"]:
        name = args[1]
        if name == "all":
            for k in ("product", "unlocked", "current-slot", "is-userspace", "secure"):
                err(f"(bootloader) {k}:{var(k)}")
            err("all:\nFinished. Total time: 0.010s")
            return 0
        value = var(name)
        if value is None:
            err(f"getvar:{name} FAILED (remote: 'GetVar Variable Not found')")
            return 1
        err(f"{name}: {value}\nFinished. Total time: 0.001s")
        return 0

    def log(entry: str) -> None:
        state.setdefault("log", []).append(entry)

    vflags = []
    while args and args[0] in ("--disable-verity", "--disable-verification"):
        vflags.append(args.pop(0))
    if args[:1] == ["flash"]:
        part, image = args[1], Path(args[2])
        data = image.read_bytes()
        if state.get("fail_flash") == part:
            err(f"Sending '{part}' OKAY\nWriting '{part}' FAILED (remote: 'Flash write failure')")
            return 1
        if part == "super":
            if len(data) > state["super_size"]:
                err("Writing 'super' FAILED (remote: 'image too large')")
                return 1
            (ROOT / "super.flashed").write_bytes(data)
        elif part in phys:
            if len(data) > phys[part]:
                err(f"Writing '{part}' FAILED (remote: 'image too large for partition')")
                return 1
            target = ROOT / "by-name" / part
            current = target.read_bytes()
            target.write_bytes(data + current[len(data):])
        elif mode == "fastbootd" and f"{part}_{slot}" in logi:
            (ROOT / "mapper" / f"{part}_{slot}").write_bytes(data)
        else:
            err(f"Writing '{part}' FAILED (remote: 'partition does not exist')")
            return 1
        log(" ".join([*vflags, "flash", part]))
        save(state)
        err(f"Sending '{part}' OKAY\nWriting '{part}' OKAY\nFinished.")
        return 0

    if args[:1] == ["reboot"] or args[0] in ("reboot-bootloader", "reboot-fastboot"):
        target = args[1] if len(args) > 1 else {"reboot-bootloader": "bootloader", "reboot-fastboot": "fastboot"}.get(args[0], "")
        if target == "fastboot":
            state["mode"] = "brick" if state.get("fastbootd_broken") else "fastbootd"
        elif target == "bootloader":
            state["mode"] = "bootloader"
        elif target == "recovery":
            state["mode"] = "recovery"
        else:
            state["mode"] = "android"
        log(f"reboot {target}".strip())
        save(state)
        err("Rebooting\nFinished.")
        return 0

    if args[:1] == ["set_active"]:
        state["slot"] = args[1]
        log(f"set_active {args[1]}")
        save(state)
        return 0
    if args in (["flashing", "unlock"], ["oem", "unlock"]):
        style = state.get("unlock_style", "flashing")
        if args[0] != style:
            err("FAILED (remote: 'unknown command')")
            return 1
        if not state.get("oem_unlock_allowed", True):
            err("FAILED (remote: 'Flashing Unlock is not allowed')")
            return 1
        log(" ".join(args))
        if state.get("owner_confirms", True):  # the owner presses UNLOCK on the phone's screen
            state["unlocked"] = True
            state["wiped"] = True
        save(state)
        err("OKAY\nFinished.")
        return 0
    if args == ["-w"]:
        state["wiped"] = True
        log("wipe")
        save(state)
        err("Erasing 'userdata' OKAY\nErasing 'metadata' OKAY")
        return 0
    if args == ["snapshot-update", "cancel"]:
        state["snapshot"] = "none"
        log("snapshot-update cancel")
        save(state)
        return 0
    err(f"fake fastboot: unsupported {args}")
    return 99


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
