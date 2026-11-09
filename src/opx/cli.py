"""Command line interface: ``opx``."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from opx import __version__
from opx import backup as bk
from opx import device as dev
from opx.profile import MODE_TEXT, MODES
from opx import plan as pn
from opx.rom import avb, qcom
from opx.rom import payload as pl
from opx.rom.ota import RomError, open_rom
from opx.session import open_session
from opx.tools import Tool, ToolNotFound, fetch_platform_tools, find_tool


def _human(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _pick_adb_serial(serial: str | None) -> str:
    scan = dev.scan()
    adb_devices = [d for d in scan.devices if d.transport == "adb"]
    if serial:
        match = [d for d in adb_devices if d.serial == serial]
        if not match:
            raise SystemExit(f"No ADB device with serial {serial}.")
        d = match[0]
    elif len(adb_devices) == 1:
        d = adb_devices[0]
    elif not adb_devices:
        raise SystemExit("No phone found over ADB. Connect it with USB debugging on, or boot a custom recovery.")
    else:
        raise SystemExit("More than one phone connected; choose one with -s SERIAL.")
    if d.state == "unauthorized":
        raise SystemExit("ADB is not authorised. Unlock the phone and accept the USB debugging prompt.")
    if not d.can_backup:
        raise SystemExit(f"Device is in '{d.mode_text}' mode; backups need Android or recovery over ADB.")
    return d.serial


def cmd_devices(args) -> int:
    scan = dev.scan()
    for err in scan.errors:
        print(f"! {err}", file=sys.stderr)
    if not scan.devices:
        print("No devices found.")
        return 1
    for d in scan.devices:
        print(f"{d.serial:<20} {d.transport:<9} {d.mode_text:<22} {d.model or d.product}")
    return 0


def cmd_info(args) -> int:
    scan = dev.scan()
    fb = [d for d in scan.devices if d.transport == "fastboot" and (not args.serial or d.serial == args.serial)]
    if fb and not [d for d in scan.devices if d.transport == "adb"]:
        facts = dev.fastboot_summary(Tool("fastboot").bind(fb[0].serial))
        print(f"Device {fb[0].serial} is in {fb[0].mode_text}")
        for k, v in facts.items():
            print(f"  {k:<20} {v}")
        print("Partition reads are not possible in fastboot; boot Android (rooted) or a recovery for backups.")
        return 0

    s = open_session(_pick_adb_serial(args.serial))
    facts = bk.device_facts(s.props)
    print(f"Serial         {s.serial}")
    for k, v in facts.items():
        print(f"{k:<15}{v}")
    print(f"{'root access':<15}{s.access.kind}")
    print(f"{'profile':<15}{s.profile.name} ({s.profile.id})")
    for issue in s.profile.issues_for(s.props):
        print(f"\n!! {issue.severity.upper()}: {issue.message}")
    return 0


def cmd_partitions(args) -> int:
    s = open_session(_pick_adb_serial(args.serial))
    plan = bk.build_plan(s.shell, s.profile, args.mode, s.props)
    _print_plan(plan)
    return 0


def _print_plan(plan: bk.Plan) -> None:
    print(f"Mode: {MODE_TEXT[plan.mode]}")
    print(f"Device: {plan.device.get('model')}  build {plan.device.get('build')}  slot {plan.device.get('slot_suffix')}")
    print(f"Root access: {plan.access}   Profile: {plan.profile}\n")
    for item in plan.items:
        print(f"  {item.name:<28} {_human(item.size):>10}   {item.path}")
    print(f"\n  Total: {_human(plan.total_bytes)} in {len(plan.items)} partition(s)")
    if plan.missing:
        print(f"  Not found on this phone: {', '.join(plan.missing)}")
    for w in plan.warnings:
        print(f"  ! {w}")


def cmd_backup(args) -> int:
    s = open_session(_pick_adb_serial(args.serial))
    plan = bk.build_plan(s.shell, s.profile, args.mode, s.props)
    _print_plan(plan)
    if args.dry_run:
        print("\nDry run: nothing was read or written.")
        return 0

    def on_event(e: dict) -> None:
        t = e["type"]
        if t == "item_start":
            print(f"\n[{e['index']}/{e['total']}] {e['name']} ({_human(e['size'])})")
        elif t == "progress" and e.get("size"):
            pct = 100 * e["done"] / e["size"]
            print(f"\r    {pct:5.1f}%  {_human(e['done'])}", end="", flush=True)
        elif t == "item_done":
            print(f"\r    {e['status'].upper():<10} {e['sha256'][:16]}...  {e['error']}")
        elif t == "log":
            print(f"    {e['message']}")
        elif t == "finished":
            print(f"\nDone: {e['ok']} verified, {e['unverified']} not verified on phone, {len(e['failed'])} failed")
            if e["failed"]:
                print(f"Failed: {', '.join(e['failed'])}")
            print(f"Saved to {e['folder']}")

    try:
        bk.run_backup(s.shell, plan, Path(args.out), verify_on_device=not args.no_verify, on_event=on_event)
    except bk.Cancelled:
        return 130
    return 0


def cmd_verify(args) -> int:
    report = bk.verify_folder(Path(args.folder))
    bad = 0
    for file, ok, msg in report:
        print(f"{'OK ' if ok else 'BAD'}  {file:<32} {msg}")
        bad += not ok
    print(f"\n{len(report) - bad} OK, {bad} problem(s)")
    return 1 if bad else 0


def cmd_tools(args) -> int:
    if args.action == "where":
        for name in ("adb", "fastboot"):
            try:
                print(f"{name:<9} {' '.join(find_tool(name))}")
            except ToolNotFound as e:
                print(f"{name:<9} not found ({e})")
        return 0
    print("Downloading Google's Android SDK Platform-Tools (Google's terms apply)...")

    def progress(done, total):
        if total:
            print(f"\r  {100 * done / total:5.1f}%", end="", flush=True)

    dest = fetch_platform_tools(progress=progress)
    print(f"\nInstalled to {dest}")
    return 0


def cmd_rom_inspect(args) -> int:
    rom = open_rom(args.path)
    summary = pn.rom_summary(rom)
    for k, v in summary.items():
        print(f"{k:<18}{v}")
    if rom.payload:
        print(f"{'payload':<18}v{rom.payload.version}, block {rom.payload.block_size}, minor {rom.payload.minor_version}")
        for g in rom.payload.groups:
            print(f"{'group':<18}{g.name} ({_human(g.size)}): {', '.join(g.partitions)}")
    print()
    for img in rom.images:
        kind = {True: "logical", False: "physical", None: "?"}[img.logical]
        print(f"  {img.name:<28} {_human(img.size):>10}  {kind}")
    for note in rom.notes:
        print(f"! {note}")
    return 0


def cmd_rom_extract(args) -> int:
    rom = open_rom(args.path)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in args.names:
        img = rom.image(name)
        if img is None:
            print(f"! {name} is not in this ROM")
            return 1
        target = out / f"{name}.img"
        if img.file is not None:
            shutil.copyfile(img.file, target)
        else:
            pl.extract(rom.payload, name, target)
        print(f"{name:<24} {_human(img.size):>10}  -> {target}  (hash verified)")
    return 0


def cmd_rom_avb(args) -> int:
    vb = avb.parse(Path(args.image).read_bytes())
    print(f"rollback index     {vb.rollback_index}")
    print(f"rollback location  {vb.rollback_index_location}")
    print(f"flags              {vb.flags}" + ("  (verification disabled)" if vb.verification_disabled else ""))
    print(f"release            {vb.release}")
    for name, loc in vb.chains:
        print(f"chain              {name} (location {loc})")
    if vb.hash_partitions:
        print(f"hash partitions    {', '.join(vb.hash_partitions)}")
    if vb.hashtree_partitions:
        print(f"hashtree           {', '.join(vb.hashtree_partitions)}")
    for k, v in vb.properties.items():
        print(f"property           {k} = {v}")
    return 0


def cmd_rom_arb(args) -> int:
    arb = qcom.read_arb(Path(args.image).read_bytes())
    print("EXPERIMENTAL - compare with known values before trusting.")
    print(f"MBN header version  {arb.mbn_version}")
    print(f"software id         {arb.software_id if arb.software_id is not None else '?'}")
    print(f"OEM ARB             {arb.oem_arb if arb.oem_arb is not None else '?'}")
    print(f"QTI ARB             {arb.qti_arb if arb.qti_arb is not None else '?'}")
    if arb.note:
        print(arb.note)
    return 0


SYMBOL = {"pass": "OK  ", "info": "i   ", "warn": "WARN", "block": "STOP"}


def cmd_plan(args) -> int:
    rom = open_rom(args.rom)
    if args.backup:
        dev_state = pn.device_from_backup(Path(args.backup))
    elif args.offline:
        dev_state = pn.no_device()
    else:
        dev_state = pn.device_from_session(open_session(_pick_adb_serial(args.serial)))
    report = pn.build_report(rom, dev_state)
    if args.json:
        print(report.to_json())
        return 0 if report.verdict != "BLOCKED" else 3
    print(f"ROM:   {rom.path}  ({report.rom['product'] or '?'} / {report.rom['codename'] or '?'}, Android {report.rom['android'] or '?'}, patch {report.rom['security_patch'] or '?'})")
    print(f"Phone: {dev_state.label}\n")
    for c in report.checks:
        print(f"  [{SYMBOL[c.status]}] {c.name:<26} {c.detail}")
    print("\nDry-run plan (nothing is written):")
    for i in report.items:
        target = i.target if i.action != "skip" else "skipped"
        extra = f"  ({i.note})" if i.note else ""
        print(f"  {i.partition:<26} {_human(i.size):>10}  {i.action:<12} {target}{extra}")
    print(f"\nVerdict: {report.verdict}")
    return 0 if report.verdict != "BLOCKED" else 3


def cmd_gui(args) -> int:
    from opx.gui.app import main as gui_main

    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="opx", description="OPxFlasher - safety-first OPPO/OnePlus tool")
    p.add_argument("--version", action="version", version=f"OPxFlasher {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list connected phones").set_defaults(fn=cmd_devices)

    info = sub.add_parser("info", help="show device facts, root access and warnings")
    info.add_argument("-s", "--serial")
    info.set_defaults(fn=cmd_info)

    parts = sub.add_parser("partitions", help="preview which partitions a backup mode would read")
    parts.add_argument("-s", "--serial")
    parts.add_argument("--mode", choices=MODES, default="critical")
    parts.set_defaults(fn=cmd_partitions)

    b = sub.add_parser("backup", help="back up partitions (read-only)")
    b.add_argument("-s", "--serial")
    b.add_argument("--mode", choices=MODES, default="critical")
    b.add_argument("--out", default="backups", help="output folder (default: ./backups)")
    b.add_argument("--dry-run", action="store_true", help="show the plan without reading anything")
    b.add_argument("--no-verify", action="store_true", help="skip the sha256sum check on the phone")
    b.set_defaults(fn=cmd_backup)

    v = sub.add_parser("verify", help="re-check a backup folder against its manifest")
    v.add_argument("folder")
    v.set_defaults(fn=cmd_verify)

    t = sub.add_parser("tools", help="find or download adb/fastboot")
    t.add_argument("action", choices=("where", "fetch"))
    t.set_defaults(fn=cmd_tools)

    rom = sub.add_parser("rom", help="look inside a ROM (OTA zip, payload.bin or image folder)")
    rsub = rom.add_subparsers(dest="rom_command", required=True)
    ri = rsub.add_parser("inspect", help="device, build, full/incremental and image list")
    ri.add_argument("path")
    ri.set_defaults(fn=cmd_rom_inspect)
    rx = rsub.add_parser("extract", help="extract images (hash-verified)")
    rx.add_argument("path")
    rx.add_argument("names", nargs="+", help="partition names, e.g. vbmeta init_boot")
    rx.add_argument("--out", default="extracted")
    rx.set_defaults(fn=cmd_rom_extract)
    ra = rsub.add_parser("avb", help="show AVB rollback index and descriptors of an image")
    ra.add_argument("image")
    ra.set_defaults(fn=cmd_rom_avb)
    rq = rsub.add_parser("arb", help="EXPERIMENTAL: show Qualcomm anti-rollback version of an image")
    rq.add_argument("image")
    rq.set_defaults(fn=cmd_rom_arb)

    pl_ = sub.add_parser("plan", help="check a ROM against the phone and show a dry-run flash plan")
    pl_.add_argument("rom", help="OTA zip, payload.bin or image folder")
    src = pl_.add_mutually_exclusive_group()
    src.add_argument("--backup", help="compare against a backup folder instead of the connected phone")
    src.add_argument("--offline", action="store_true", help="no phone or backup (everything device-specific is blocked)")
    pl_.add_argument("-s", "--serial")
    pl_.add_argument("--json", action="store_true", help="machine-readable report")
    pl_.set_defaults(fn=cmd_plan)

    sub.add_parser("gui", help="open the graphical app").set_defaults(fn=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (bk.BackupError, ToolNotFound, RomError, pl.PayloadError, avb.AvbError, qcom.QcomError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
