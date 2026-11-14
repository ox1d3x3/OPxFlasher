"""Command line interface: ``opx``."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from opx import __version__
from opx import backup as bk
from opx import device as dev
from opx import plan as pn
from opx.profile import MODE_TEXT, MODES
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


def cmd_ota(args) -> int:
    from opx import catalog

    if args.action == "snapshot":
        out = Path(args.out if args.out != "." else "opx-catalog-snapshot.json")
        if out.is_dir():
            out = out / "opx-catalog-snapshot.json"
        try:
            res = catalog.snapshot(out, progress=lambda d, i, n: print(f"\r  {i}/{n} {d:<20}", end="", flush=True))
        except catalog.CatalogError as e:
            print(f"\n{e}")
            return 1
        print(f"\nSaved {res['count']} releases for {res['devices']} devices to {res['path']}")
        print(f"{catalog.CREDIT}. Use it when the catalog is down: OPxFlasher > Tools > Use a ROM catalog snapshot.")
        return 0

    model = args.model
    if not (model or args.device):
        model = open_session(_pick_adb_serial(args.serial)).props.get("ro.product.model", "")
        if not model:
            print("Can't read the phone's model; pass --model (e.g. PJZ110) or --device (e.g. 'OP 13').")
            return 2
    try:
        releases = catalog.fetch(model=model or "", device=args.device or "",
                                 region=args.region or "", latest=args.latest)
    except catalog.CatalogError as e:
        print(e)
        return 1

    if args.action == "list":
        if not releases:
            print("The catalog has no releases for that filter.")
            return 1
        print(f"{'id':<26} {'model':<8} {'reg':<4} {'version':<24} {'patch':<11} size")
        for r in releases:
            mark = "  (latest)" if r.is_latest else ""
            print(f"{r.id:<26} {r.model:<8} {r.region:<4} {r.version:<24} "
                  f"{r.security_patch or '-':<11} {r.size_text}{mark}")
        print(f"\n{catalog.CREDIT}")
        return 0

    match = [r for r in releases if r.id == args.id] if args.id else [r for r in releases if r.is_latest]
    if len(match) != 1:
        print("Pick one release with --id (see 'opx ota list').")
        return 2
    rel = match[0]
    print(f"Downloading {rel.device} {rel.region} {rel.version} ({rel.size_text}) from the official server...")
    last = [-1]

    def show(done, total):
        pct = int(done * 100 / total) if total else 0
        if pct != last[0]:
            last[0] = pct
            print(f"\r  {pct:3d}%  {done / 1e6:,.0f} MB", end="", flush=True)

    def step(text):
        print(f"\n  {text}", flush=True)
        last[0] = -1

    try:
        path = catalog.download(rel, Path(args.out), progress=show, stage=step,
                                connections=max(1, min(args.connections, 8)))
    except catalog.DownloadPaused as e:
        print(f"\n{e}")
        return 3
    except catalog.CatalogError as e:
        print(f"\n{e}")
        return 1
    print(f"\nSaved {path}")
    print(f"Next: opx rom inspect {path}")
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
    if args.all or args.csv:
        from opx import explorer as ex

        try:
            rows = ex.explore(s.shell, s.profile, s.props)
        except ex.ExplorerError as e:
            print(e)
            return 2
        if args.csv:
            Path(args.csv).write_text(ex.to_csv(rows), "utf-8")
            print(f"Saved {len(rows)} partitions to {args.csv}")
            return 0
        for r in rows:
            print(f"  {r.name:<28} {_human(r.size):>10}  {r.kind:<9} {r.role}")
        print(f"\n  {len(rows)} partitions (read-only listing)")
        return 0
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


def cmd_rom_detect(args) -> int:
    from opx.rom import detect

    d = detect.detect(args.path)
    print("\n".join(d.lines()))
    return 0 if d.ok else 1


def cmd_rom_convert(args) -> int:
    from opx import convert as cv
    from opx.rom import detect

    d = detect.detect(args.path)
    print("\n".join(d.lines()) + "\n")
    ok, why = d.convertible
    if not ok:
        print(f"Can't convert: {why}")
        return 1
    last = [-1]

    def show(what, done, total):
        pct = int(done * 100 / total) if total else 0
        if pct != last[0]:
            last[0] = pct
            print(f"\r  {pct:3d}%  {what:<24}", end="", flush=True)

    def step(text):
        print(f"\n  {text}", flush=True)
        last[0] = -1

    try:
        made = cv.convert(d.rom, Path(args.out), make_zip=not args.no_zip, make_folder=args.folder,
                          progress=show, stage=step)
    except (cv.ConvertError, OSError) as e:
        print(f"\n{e}")
        return 1
    print()
    if "zip" in made:
        print(f"Recovery zip: {made['zip']}")
        print("  Flash it in OrangeFox / TWRP (or AERA). It checks the phone, the variant and that the ROM")
        print("  isn't older than the installed Android before writing anything; your data is kept.")
    if "folder" in made:
        print(f"Image folder: {made['folder']}  (ROM check / flash it with OPxFlasher, or fastboot)")
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
    _print_report(rom, report, dev_state)
    print(f"\nVerdict: {report.verdict}")
    return 0 if report.verdict != "BLOCKED" else 3


def _print_report(rom, report, dev_state) -> None:
    r = report.rom
    print(f"ROM:   {rom.path}  ({r['product'] or '?'} / {r['codename'] or '?'}, Android {r['android'] or '?'}, patch {r['security_patch'] or '?'})")
    print(f"Phone: {dev_state.label}\n")
    for c in report.checks:
        print(f"  [{SYMBOL[c.status]}] {c.name:<26} {c.detail}")
    print("\nDry-run plan (nothing is written):")
    for i in report.items:
        target = i.target if i.action != "skip" else "skipped"
        extra = f"  ({i.note})" if i.note else ""
        print(f"  {i.partition:<26} {_human(i.size):>10}  {i.action:<12} {target}{extra}")


def _confirm(word: str, prompt: str) -> bool:
    try:
        answer = input(f"{prompt}\nType {word} to continue (anything else cancels): ")
    except EOFError:
        return False
    return answer.strip() == word


def _print_flash_event(e: dict) -> None:
    t = e["type"]
    if t == "extract":
        print(f"  preparing image {e['index']}/{e['total']}: {e['name']}")
    elif t == "step_start":
        print(f"[{e['index']}/{e['total']}] {e['label']} ...", flush=True)
    elif t == "step_done":
        print(f"    {e['status'].upper()}: {e['detail']}")
    elif t == "log":
        print(f"    {e['message']}")
    elif t == "finished":
        print(f"\nFlash complete. Journal: {e['journal']}")


def cmd_flash(args) -> int:
    from opx import flash as fl
    from opx import profile as profiles

    if args.resume:
        plan = fl.load_journal(Path(args.resume))
        print(f"Resuming flash of {plan.rom_path} on {plan.device.get('model')} ({plan.device.get('serial')})")
        for i, st in enumerate(plan.steps, 1):
            print(f"  {i:>2}. [{st.status:<7}] {st.label}")
        if not _confirm("RESUME", "Finished steps are skipped; the rest run in order."):
            print("Cancelled. Nothing was written.")
            return 1
        profile = profiles.detect({"ro.product.model": plan.device.get("model", "")})
        runner = fl.resume_runner(Path(args.resume), profile, Tool("adb"), Tool("fastboot"), on_event=_print_flash_event)
        try:
            runner.run()
        except fl.FlashError as e:
            print(f"\n{e}", file=sys.stderr)
            return 4
        return 0

    if not args.rom:
        raise SystemExit("Give a ROM to flash, or --resume JOURNAL.")
    rom = open_rom(args.rom)
    session = open_session(_pick_adb_serial(args.serial))
    dev_state = pn.device_from_session(session)
    report = pn.build_report(rom, dev_state)
    _print_report(rom, report, dev_state)

    overrides = []
    if args.accept_unvalidated_arb and fl.overridable_blocks(report, fl.OVERRIDE_QCOM_UNVALIDATED):
        overrides.append(fl.OVERRIDE_QCOM_UNVALIDATED)
    variant = fl.overridable_blocks(report, fl.OVERRIDE_VARIANT)
    if variant and args.accept_variant_change:
        overrides.append(fl.OVERRIDE_VARIANT)
    options = fl.FlashOptions(
        include_optional=[p for p in (args.include or "").split(",") if p],
        wipe_data=args.wipe,
        reboot_after=not args.no_reboot,
        allow_fastbootd=args.allow_fastbootd,
        overrides=overrides,
    )
    backup = Path(args.backup) if args.backup else None
    if fl.region_change(report) and not args.no_restore_region:
        options.restore_from_backup = [
            n for n in session.profile.conditional
            if fl.backup_images(backup, n) and n not in options.include_optional
        ]
        if options.restore_from_backup:
            print("\nRegion change: your phone's own " + ", ".join(options.restore_from_backup)
                  + " will be restored from the backup after flashing (keeps mobile signal).")
    plan = fl.prepare(rom, report, dev_state, options, backup)

    print("\nFlash steps:")
    for i, st in enumerate(plan.steps, 1):
        print(f"  {i:>2}. {st.label}")
    if not plan.ok:
        print("\nFlashing is BLOCKED:")
        for p in plan.problems:
            print(f"  - {p}")
        if fl.overridable_blocks(report, fl.OVERRIDE_QCOM_UNVALIDATED) and not args.accept_unvalidated_arb:
            print("  (The Qualcomm ARB block can be overridden with --accept-unvalidated-arb once you have checked the numbers yourself.)")
        if variant and not args.accept_variant_change:
            print("  (To flash another variant of your phone on purpose, add --accept-variant-change and --wipe.)")
        return 3
    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0

    if variant and not _confirm(
        "CHANGE VARIANT",
        "\n" + variant[0].detail.replace(" Confirm the variant change to continue.", ""),
    ):
        print("Cancelled. Nothing was written.")
        return 1
    if fl.OVERRIDE_QCOM_UNVALIDATED in overrides and not _confirm(
        "ARB CHECKED",
        "\nWARNING: the Qualcomm anti-rollback reader is not validated for this phone.\n"
        "Flashing firmware with a lower ARB than the phone's hard-bricks it permanently.\n"
        "Only continue if you have confirmed the ROM's ARB is equal to or higher than the phone's.",
    ):
        print("Cancelled. Nothing was written.")
        return 1
    wipe_note = " ALL DATA ON THE PHONE WILL BE ERASED." if args.wipe else ""
    if not _confirm("FLASH", f"\nReady to flash {rom.path.name} to {dev_state.label}.{wipe_note}"):
        print("Cancelled. Nothing was written.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = Path(args.work or ".") / f"opx-flash-{stamp}"
    work.mkdir(parents=True)
    runner = fl.Runner(plan, rom, session.profile, Tool("adb"), Tool("fastboot"), work / "journal.json", work / "images",
                       on_event=_print_flash_event)
    try:
        runner.run()
    except fl.Cancelled as e:
        print(f"\n{e}", file=sys.stderr)
        return 130
    except fl.FlashError as e:
        print(f"\n{e}", file=sys.stderr)
        return 4
    return 0


def cmd_region_show(args) -> int:
    from opx import flash as fl
    from opx import profile as profiles

    manifest = json.loads((Path(args.backup) / "manifest.json").read_text("utf-8"))
    dev = manifest.get("device", {})
    print(f"Backup of {dev.get('model')} ({dev.get('serial') or 'serial not recorded'}), build {dev.get('build')}")
    profile = profiles.load(manifest.get("profile") or "oplus-generic")
    found = 0
    for name in profile.conditional:
        for full, (path, sha, src) in sorted(fl.backup_images(args.backup, name).items()):
            ok = path.exists() and fl.sha256_file(path) == sha
            kind = "logical" if src == "mapper" else "physical"
            print(f"  {'OK ' if ok else 'BAD'}  {full:<20} {_human(path.stat().st_size if path.exists() else None):>10}  {kind}")
            found += 1
    if not found:
        print("  No region files in this backup.")
        return 1
    return 0


def cmd_region_restore(args) -> int:
    from opx import flash as fl

    session = open_session(_pick_adb_serial(args.serial))
    dev_state = pn.device_from_session(session)
    if args.only:
        names = [n.strip() for n in args.only.split(",") if n.strip()]
    else:  # every region file the backup actually has
        names = [n for n in session.profile.conditional if fl.backup_images(args.backup, n)]
    options = fl.FlashOptions(
        restore_from_backup=names,
        allow_fastbootd=args.allow_fastbootd,
        reboot_after=not args.no_reboot,
    )
    plan = fl.prepare_restore(dev_state, Path(args.backup), options)
    print(f"Restore region files to {dev_state.label} from {args.backup}\n")
    for i, st in enumerate(plan.steps, 1):
        print(f"  {i:>2}. {st.label}")
    if not plan.ok:
        print("\nRestore is BLOCKED:")
        for prob in plan.problems:
            print(f"  - {prob}")
        return 3
    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0
    if not _confirm("RESTORE", "\nThis writes your backed-up region files back to the phone."):
        print("Cancelled. Nothing was written.")
        return 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = Path(args.work or ".") / f"opx-restore-{stamp}"
    work.mkdir(parents=True)
    runner = fl.Runner(plan, None, session.profile, Tool("adb"), Tool("fastboot"), work / "journal.json", work / "images",
                       on_event=_print_flash_event)
    try:
        runner.run()
    except fl.Cancelled as e:
        print(f"\n{e}", file=sys.stderr)
        return 130
    except fl.FlashError as e:
        print(f"\n{e}", file=sys.stderr)
        return 4
    return 0


PATCH_HINT = {
    "magisk": "Open Magisk -> Install -> 'Select and Patch a File' -> choose Download/{name} -> Let's Go.",
    "kernelsu": "Open KernelSU -> Install -> 'Select a file' -> choose Download/{name} (LKM mode) -> Next.",
    "apatch": "Open APatch -> Patch -> 'Select a boot image' -> choose Download/{name}, set your superkey -> Start.",
}


def _root_session_tools(session):
    serial = session.device.get("serial") or _pick_adb_serial(None)
    return Tool("adb").bind(serial)


def cmd_root_prepare(args) -> int:
    from opx import root as rt

    session_live = open_session(_pick_adb_serial(args.serial))
    device = pn.device_from_session(session_live)
    if not session_live.access.usable:
        print("Note: root isn't needed for this step.")
    partition = rt.MANAGERS[args.manager][1]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = Path(args.work or f"opx-root-{device.facts.get('model') or 'phone'}-{stamp}")
    if args.rom:
        stock = rt.stock_from_rom(open_rom(args.rom), partition, device.facts, work)
        source = f"ROM {Path(args.rom).name}"
    else:
        stock = rt.stock_from_backup(Path(args.backup), partition, device.facts, work)
        source = f"backup {Path(args.backup).name}"
    session = rt.new_session(args.manager, device, stock, source)
    remote = rt.push_stock(session_live.adb, session)
    session.save(work)
    name = Path(remote).name
    print(f"Stock {partition} for build {device.facts.get('build')} (from {source}) verified and sent to the phone:")
    print(f"  {remote}\n")
    print("Now patch it on the phone:")
    print("  " + PATCH_HINT[args.manager].format(name=name))
    print(f"\nThen run:  opx root pull --work \"{work}\"")
    return 0


def cmd_root_pull(args) -> int:
    from opx import root as rt

    work = Path(args.work)
    session = rt.RootSession.load(work)
    problems = rt.pull_patched(_root_session_tools(session), session, work)
    session.save(work)
    print(f"Pulled {session.patched_remote} -> {session.patched_path}")
    if problems:
        print("\nThe patched image doesn't look right:")
        for prob in problems:
            print(f"  - {prob}")
        return 3
    print("Checks passed (valid boot image, same header version as stock, actually patched).")
    print(f"\nNext:  opx root flash --work \"{work}\" --backup <your backup>")
    return 0


def _root_flash(args, purpose: str, word: str) -> int:
    from opx import flash as fl
    from opx import root as rt

    work = Path(args.work)
    session = rt.RootSession.load(work)
    live = open_session(_pick_adb_serial(args.serial or session.device.get("serial")))
    device = pn.device_from_session(live)
    plan = rt.prepare_flash(device, Path(args.backup) if args.backup else None, session, purpose, not args.no_reboot)
    print(f"{'Root' if purpose == 'root' else 'Unroot'} {device.label} ({rt.MANAGERS[session.manager][0]}, {session.partition}):")
    for i, st in enumerate(plan.steps, 1):
        print(f"  {i:>2}. {st.label}")
    if not plan.ok:
        print("\nBLOCKED:")
        for prob in plan.problems:
            print(f"  - {prob}")
        return 3
    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0
    if not _confirm(word, "\nOnly the current slot is written; the other slot keeps its image."):
        print("Cancelled. Nothing was written.")
        return 1
    journal = work / f"{purpose}-journal.json"
    runner = fl.Runner(plan, None, live.profile, Tool("adb"), Tool("fastboot"), journal, work / "images",
                       on_event=_print_flash_event)
    try:
        runner.run()
    except fl.FlashError as e:
        print(f"\n{e}", file=sys.stderr)
        return 4
    return 0


def cmd_root_flash(args) -> int:
    return _root_flash(args, "root", "ROOT")


def cmd_root_unroot(args) -> int:
    return _root_flash(args, "unroot", "UNROOT")


def _phone_for_reboot(serial: str | None):
    from opx import profile as profiles

    scan = dev.scan()
    usable = [d for d in scan.devices
              if (d.transport == "adb" and d.state in ("device", "recovery"))
              or (d.transport == "fastboot" and d.state in ("bootloader", "fastbootd"))]
    if serial:
        usable = [d for d in usable if d.serial == serial]
    if len(usable) != 1:
        for err in scan.errors:
            print(err)
        print("No phone found." if not usable else "Several phones connected; pick one with -s SERIAL.")
        return None, None
    d = usable[0]
    if d.transport != "adb":
        return d, None  # build unknown in fastboot
    tool = Tool("adb").bind(d.serial)
    props = {k: tool.run("shell", "getprop", k, timeout=15).stdout.strip()
             for k in ("ro.product.model", "ro.build.display.id")}
    return d, profiles.detect(props).blocked_actions(props)


def cmd_reboot(args) -> int:
    from opx import reboot as rb

    d, blocked = _phone_for_reboot(args.serial)
    if d is None:
        return 2
    choice = next(c for c in rb.choices(d, blocked) if c.target == args.target)
    if not choice.allowed:
        print(f"Not rebooting: {choice.reason}")
        return 3
    if choice.confirm and not args.yes and not _confirm("YES", choice.reason):
        print("Cancelled.")
        return 1
    try:
        print(rb.reboot(d, args.target, blocked))
    except rb.RebootError as e:
        print(e, file=sys.stderr)
        return 4
    return 0


def cmd_restore_rom(args) -> int:
    from opx import flash as fl
    from opx import restore as rs

    folder = Path(args.backup)
    scan = dev.scan()
    live_phone = [d for d in scan.devices if d.transport == "adb" and d.state == "device"]
    fb_phone = [d for d in scan.devices if d.transport == "fastboot"]
    if args.serial:
        live_phone = [d for d in live_phone if d.serial == args.serial]
        fb_phone = [d for d in fb_phone if d.serial == args.serial]
    try:
        if live_phone:
            session = open_session(live_phone[0].serial)
            device, profile, live = pn.device_from_session(session), session.profile, True
        elif len(fb_phone) == 1:
            device = rs.device_in_fastboot(folder, fb_phone[0].serial)
            profile, live = device.profile, False
        else:
            print("Connect the phone in Android (USB debugging) or in fastboot (one phone only).")
            return 2
        overrides = []
        if args.not_updated:
            overrides.append(fl.OVERRIDE_UNKNOWN_FIRMWARE)
        if args.accept_unvalidated_arb:
            overrides.append(fl.OVERRIDE_QCOM_UNVALIDATED)
        rom, report, plan = rs.prepare_restore_rom(folder, device, live, args.wipe, overrides, args.allow_fastbootd)
    except rs.RestoreError as e:
        print(e)
        return 2
    print(rom.notes[-1])
    _print_report(rom, report, device)
    print("\nRestore steps:")
    for i, st in enumerate(plan.steps, 1):
        print(f"  {i:>2}. {st.label}")
    if not plan.ok:
        print("\nRestore is BLOCKED:")
        for p in plan.problems:
            print(f"  - {p}")
        if fl.overridable_blocks(report, fl.OVERRIDE_UNKNOWN_FIRMWARE) and not args.not_updated:
            print("  (If you haven't installed any update since this backup, add --not-updated.)")
        return 3
    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0
    if args.not_updated and not _confirm(
        "NOT UPDATED",
        "\nYou're confirming the phone has NOT installed any update since this backup. "
        "If it has, restoring older firmware can hard-brick it.",
    ):
        print("Cancelled. Nothing was written.")
        return 1
    if args.accept_unvalidated_arb and not _confirm(
        "ARB CHECKED", "\nThe Qualcomm anti-rollback reader isn't validated for this phone yet."):
        print("Cancelled. Nothing was written.")
        return 1
    wipe = " ALL DATA ON THE PHONE WILL BE ERASED." if args.wipe else " Your data stays."
    if not _confirm("RESTORE ROM", f"\nThis replaces the phone's OS with the backup.{wipe}"):
        print("Cancelled. Nothing was written.")
        return 1
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    runner = fl.Runner(plan, rom, profile, Tool("adb"), Tool("fastboot"), work / "restore-journal.json",
                       work / "images", on_event=_print_flash_event)
    try:
        runner.run()
    except fl.FlashError as e:
        print(f"\n{e}", file=sys.stderr)
        return 4
    print("\nRestore finished.")
    return 0


def cmd_apps(args) -> int:
    from opx import apps as ap

    s = open_session(_pick_adb_serial(args.serial))
    keep = s.profile.keep_apps
    try:
        if args.action == "list":
            mine = set(ap.removed_by_us(s.serial))
            for a in ap.list_system_apps(s.adb, keep):
                if args.removed and not a.removed:
                    continue
                if args.search and args.search.lower() not in a.package.lower():
                    continue
                state = "removed" if a.removed else ("disabled" if a.disabled else "installed")
                extra = " (by OPxFlasher)" if a.package in mine else ""
                lock = f"  [keep: {a.keep}]" if a.keep else ""
                print(f"  {a.package:<52} {state}{extra}{lock}")
            return 0
        if args.action == "export":
            if not args.packages:
                print("Give the file to write, e.g. opx apps export my-debloat.txt")
                return 2
            done = ap.removed_by_us(s.serial)
            ap.write_list(Path(args.packages[0]), done, f"Removed on {s.props.get('ro.product.model', '')}")
            print(f"Saved {len(done)} package(s) to {args.packages[0]}")
            return 0

        packages = list(args.packages)
        if args.file:
            packages += ap.read_list(Path(args.file))
        if args.action == "restore" and args.all:
            packages += ap.removed_by_us(s.serial)
        packages = list(dict.fromkeys(packages))
        if not packages:
            print("No packages given.")
            return 2
        if args.action == "remove":
            known = {a.package: a for a in ap.list_system_apps(s.adb, keep)}
            todo, skipped = [], []
            for p in packages:
                a = known.get(p)
                if a is None:
                    skipped.append(f"{p}: not a system app on this phone")
                elif a.removed:
                    skipped.append(f"{p}: already removed")
                elif a.keep:
                    skipped.append(f"{p}: kept ({a.keep.lower()})")
                else:
                    todo.append(p)
            for line in skipped:
                print(f"  skip  {line}")
            if not todo:
                print("Nothing to remove.")
                return 0
            print("Will remove for your user (undo with 'opx apps restore --all'):")
            for p in todo:
                print(f"  - {p}")
            try:
                answer = "y" if args.yes else input("Continue? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return 1
            packages = todo
        failed = 0
        for p in packages:
            try:
                if args.action == "remove":
                    ap.remove(s.adb, s.serial, p, keep)
                else:
                    ap.restore(s.adb, s.serial, p)
                print(f"  ok    {p}")
            except ap.AppError as e:
                failed += 1
                print(f"  fail  {e}")
        return 1 if failed else 0
    except ap.AppError as e:
        print(e)
        return 2


def cmd_logs(args) -> int:
    from opx import bootdiag, bootlogs

    if args.read:
        try:
            print(bootdiag.report(bootdiag.analyze(bootdiag.read_logs(Path(args.read)))))
        except (OSError, zipfile.BadZipFile) as e:
            print(f"Can't read {args.read}: {e}")
            return 2
        return 0
    s = open_session(_pick_adb_serial(args.serial))
    out = bootlogs.collect(s, Path(args.out), emit=lambda text: print(f"  {text}..."))
    print(f"Saved {out}\n")
    print(bootdiag.report(bootdiag.analyze(bootdiag.read_logs(out))))
    return 0


def cmd_rescue(args) -> int:
    from opx import rescue

    a = rescue.gather([Path(p) for p in args.backups])
    print(f"{a.title}\n{a.summary}\n")
    commands = {"usb-check": "opx usb-check", "identify-9008": "opx edl-info", "save-logs": "opx logs",
                "restore-rom": "opx restore-rom <backup folder>", "restore-zip": "opx restore-zip <backup folder>",
                "official-roms": "opx ota", "check-again": "opx rescue"}
    for i, step in enumerate(a.steps, 1):
        how = commands.get(step.action) or (f"opx reboot {step.action.split(':', 1)[1]}"
                                            if step.action.startswith("reboot:") else "")
        print(f"  {i}. {step.text}" + (f"\n     -> {how}" if how else ""))
    return 0


def cmd_edl_info(args) -> int:
    from opx import sahara

    try:
        info = sahara.identify_port(args.port) if args.port else sahara.identify_first()
    except sahara.SaharaError as e:
        print(e)
        return 2
    for line in info.lines():
        print(f"  {line}")
    return 0


def cmd_about(args) -> int:
    from opx import about

    print(about.text())
    print(f"Releases: {about.RELEASES_URL}")
    print(f"Report a problem: {about.ISSUES_URL}")
    print("Support: " + ", ".join(f"{name} {url}" for name, url in about.SUPPORT))
    return 0


def cmd_usb_check(args) -> int:
    from opx import usbcheck

    worst = 0
    for f in usbcheck.check():
        mark = {"ok": "OK  ", "warning": "NOTE", "problem": "FIX "}[f.level]
        print(f"[{mark}] {f.message}")
        worst = max(worst, {"ok": 0, "warning": 1, "problem": 2}[f.level])
    return 1 if worst == 2 else 0


def cmd_profile(args) -> int:
    import shutil

    from opx import profile as profiles

    if args.action == "list":
        builtin = profiles._builtin_ids()
        for pid in profiles.all_profile_ids():
            p = profiles.load(pid)
            where = "built-in" if pid in builtin else "user"
            models = ", ".join(sorted({m for v in p.match.values() for m in v})) or "-"
            print(f"{pid:<20} {p.name:<22} {where:<8} {models}")
        print(f"\nUser profiles folder: {profiles.user_profiles_dir()}")
        return 0
    if not args.file:
        print("Give a profile .json file.")
        return 2
    errors = profiles.check_file(Path(args.file))
    if errors:
        print(f"{args.file} has problems:")
        for e in errors:
            print(f"  - {e}")
        return 3
    print(f"{args.file} looks good.")
    if args.action == "install":
        import json

        pid = json.loads(Path(args.file).read_text("utf-8"))["id"]
        if pid in profiles._builtin_ids():
            print(f"'{pid}' is a built-in profile and can't be replaced; pick another id.")
            return 3
        dest = profiles.user_profiles_dir() / f"{pid}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.file, dest)
        print(f"Installed to {dest}")
    return 0


def cmd_restore_zip(args) -> int:
    from opx import restore as rs
    from opx import restore_zip as rz

    last = [-1]

    def show(what, done, total):
        pct = int(done * 100 / total) if total else 100
        if pct != last[0]:
            last[0] = pct
            print(f"\r  {pct:3d}%  {what:<24}", end="", flush=True)

    try:
        out = rz.build(Path(args.backup), Path(args.out), compress=args.compress, progress=show,
                       recovery=args.recovery)
    except rs.RestoreError as e:
        print(f"\n{e}")
        return 2
    print(f"\nSaved {out}")
    print(f"Flash it in {rz.RECOVERIES[args.recovery]} (Install -> the zip). "
          "It's ONLY for the phone this backup came from.")
    return 0


def cmd_flash_image(args) -> int:
    from opx import flash as fl
    from opx import single as sg

    backup = Path(args.backup) if args.backup else None
    live = None
    scan = dev.scan()
    if any(d.transport == "adb" and d.state == "device" for d in scan.devices):
        live = open_session(_pick_adb_serial(args.serial))
        device = pn.device_from_session(live)
    elif backup and (backup / "manifest.json").exists():
        device = pn.device_from_backup(backup)  # phone already in fastboot: use the backup's facts
    else:
        print("Connect the phone in Android (USB debugging), or pass --backup so OPxFlasher knows the phone.")
        return 2
    plan, warnings = sg.prepare_image_flash(device, backup, args.partition, Path(args.image), args.slot,
                                            args.disable_verity, not args.no_reboot)
    print(f"Flash one image on {device.label}:")
    for i, st in enumerate(plan.steps, 1):
        print(f"  {i:>2}. {st.label}")
    for w in warnings:
        print(f"  ! {w}")
    if not plan.ok:
        print("\nBLOCKED:")
        for prob in plan.problems:
            print(f"  - {prob}")
        return 3
    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0
    if not _confirm("FLASH", f"\nThis writes {Path(args.image).name} to {args.partition} ({args.slot} slot)."):
        print("Cancelled. Nothing was written.")
        return 1
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    profile = live.profile if live else device.profile
    runner = fl.Runner(plan, None, profile, Tool("adb"), Tool("fastboot"), work / "flash-image-journal.json",
                       work / "images", on_event=_print_flash_event)
    try:
        runner.run()
    except fl.FlashError as e:
        print(f"\n{e}", file=sys.stderr)
        return 4
    return 0


def cmd_gui(args) -> int:
    from opx.gui.app import main as gui_main

    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="opx", description="OPxFlasher - AIO tool for OnePlus / OPPO devices")
    p.add_argument("--version", action="version", version=f"OPxFlasher {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list connected phones").set_defaults(fn=cmd_devices)

    info = sub.add_parser("info", help="show device facts, root access and warnings")
    info.add_argument("-s", "--serial")
    info.set_defaults(fn=cmd_info)

    parts = sub.add_parser("partitions", help="preview which partitions a backup mode would read")
    parts.add_argument("-s", "--serial")
    parts.add_argument("--mode", choices=MODES, default="critical")
    parts.add_argument("--all", action="store_true", help="list every partition with its size and role")
    parts.add_argument("--csv", help="save the full list as a CSV file")
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

    ota = sub.add_parser("ota", help="official ROMs from Daniel Springer's OTA catalog")
    ota.add_argument("action", choices=("list", "download", "snapshot"),
                     help="snapshot: save the whole catalog to a file (--out), for when it's down")
    ota.add_argument("--model", help="model number, e.g. PJZ110 (default: the connected phone's)")
    ota.add_argument("--device", help="device name as in the catalog, e.g. 'OP 13'")
    ota.add_argument("--region", help="region code, e.g. CN, EU, IN, GLO")
    ota.add_argument("--latest", action="store_true", help="only the newest release per region")
    ota.add_argument("--id", help="release id to download (default: the latest)")
    ota.add_argument("--out", default=".", help="folder to save the zip in")
    ota.add_argument("--connections", type=int, default=4,
                     help="parallel connections for the download (1-8, default 4); resumes if interrupted")
    ota.add_argument("-s", "--serial")
    ota.set_defaults(fn=cmd_ota)

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
    rd = rsub.add_parser("detect", help="what is this file? type, phone, build, contents, what's missing")
    rd.add_argument("path")
    rd.set_defaults(fn=cmd_rom_detect)
    rc = rsub.add_parser("convert", help="make a full ROM: a recovery zip (OrangeFox / TWRP / AERA) "
                                         "and/or an image folder")
    rc.add_argument("path", help="full OTA zip, payload.bin, image folder or flasher-kit zip")
    rc.add_argument("--out", default=".", help="folder to write to")
    rc.add_argument("--folder", action="store_true", help="also write an image folder (fastboot / OPxFlasher)")
    rc.add_argument("--no-zip", action="store_true", help="don't write the recovery zip (with --folder)")
    rc.set_defaults(fn=cmd_rom_convert)
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

    rbt = sub.add_parser("reboot", help="reboot to Android, bootloader, fastbootd or recovery")
    rbt.add_argument("target", choices=("system", "bootloader", "fastbootd", "recovery"))
    rbt.add_argument("-s", "--serial")
    rbt.add_argument("--yes", action="store_true", help="skip the extra question for fastbootd from fastboot")
    rbt.set_defaults(fn=cmd_reboot)

    rr = sub.add_parser("restore-rom", help="restore the whole OS from a Full firmware backup of this phone")
    rr.add_argument("backup", help="the Full firmware backup folder")
    rr.add_argument("--wipe", action="store_true", help="erase data (needed when going back to an older build)")
    rr.add_argument("--not-updated", action="store_true",
                    help="phone can't boot: confirm it hasn't installed an update since the backup")
    rr.add_argument("--accept-unvalidated-arb", action="store_true")
    rr.add_argument("--allow-fastbootd", action="store_true")
    rr.add_argument("--dry-run", action="store_true", help="show the checks and steps only")
    rr.add_argument("--work", default="opx-work", help="folder for the journal")
    rr.add_argument("-s", "--serial")
    rr.set_defaults(fn=cmd_restore_rom)

    ap_ = sub.add_parser("apps", help="debloat: remove system apps for your user, or put them back")
    ap_.add_argument("action", choices=["list", "remove", "restore", "export"])
    ap_.add_argument("packages", nargs="*", help="package names (export: the file to write)")
    ap_.add_argument("-s", "--serial")
    ap_.add_argument("--from", dest="file", help="a debloat list file: one package per line")
    ap_.add_argument("--all", action="store_true", help="restore: everything OPxFlasher removed on this phone")
    ap_.add_argument("--removed", action="store_true", help="list: only removed apps")
    ap_.add_argument("--search", help="list: only names containing this")
    ap_.add_argument("-y", "--yes", action="store_true", help="remove without asking")
    ap_.set_defaults(fn=cmd_apps)

    lg = sub.add_parser("logs", help="save Android, kernel and last-crash logs as a zip (serials hidden)")
    lg.add_argument("-s", "--serial")
    lg.add_argument("--out", default=".", help="folder to save the zip in")
    lg.add_argument("--read", metavar="ZIP", help="don't connect: look for known problems in a saved logs zip or folder")
    lg.set_defaults(fn=cmd_logs)

    rs_ = sub.add_parser("rescue", help="phone won't start? see what state it's in and what to try")
    rs_.add_argument("--backups", nargs="*", default=["backups", str(Path.home() / "OPxFlasher-backups")],
                     help="folders with your backups (to offer a restore)")
    rs_.set_defaults(fn=cmd_rescue)

    ei = sub.add_parser("edl-info", help="identify a phone in 9008 (EDL) mode (read-only, writes nothing)")
    ei.add_argument("--port", help="serial port (default: the first 9008 port found)")
    ei.set_defaults(fn=cmd_edl_info)

    ab = sub.add_parser("about", help="version, developer and links")
    ab.set_defaults(fn=cmd_about)

    us = sub.add_parser("usb-check", help="phone not showing up? check USB drivers / permissions")
    us.set_defaults(fn=cmd_usb_check)

    pr = sub.add_parser("profile", help="list, check or install device profiles (adding a new phone)")
    pr.add_argument("action", choices=("list", "check", "install"))
    pr.add_argument("file", nargs="?", help="profile .json (check / install)")
    pr.set_defaults(fn=cmd_profile)

    rz_ = sub.add_parser("restore-zip", help="make a recovery-flashable restore zip from a Full firmware backup")
    rz_.add_argument("backup", help="the Full firmware backup folder")
    rz_.add_argument("--out", default=".", help="folder to save the zip in")
    rz_.add_argument("--compress", action="store_true", help="smaller zip, slower to make")
    rz_.add_argument("--recovery", choices=["aera", "orangefox"], default="aera",
                     help="aera (AERA image zip, default) or orangefox (OrangeFox / TWRP flashable zip)")
    rz_.set_defaults(fn=cmd_restore_zip)

    fi = sub.add_parser("flash-image", help="flash one image (boot, init_boot, recovery, vbmeta, dtbo, ...)")
    fi.add_argument("partition", help="partition name without slot suffix, e.g. boot, recovery, vbmeta")
    fi.add_argument("image", help="the .img file")
    fi.add_argument("--backup", help="a Critical or Full backup of this phone (required)")
    fi.add_argument("--slot", choices=("current", "other", "both"), default="current")
    fi.add_argument("--disable-verity", action="store_true", help="vbmeta only: flash with verity/verification off")
    fi.add_argument("--no-reboot", action="store_true", help="stay in the bootloader afterwards")
    fi.add_argument("--dry-run", action="store_true", help="show the plan and checks only")
    fi.add_argument("--work", default="opx-work", help="folder for the journal")
    fi.add_argument("-s", "--serial")
    fi.set_defaults(fn=cmd_flash_image)

    f = sub.add_parser("flash", help="flash a ROM after every safety check passes (writes to the phone)")
    f.add_argument("rom", nargs="?", help="OTA zip, payload.bin or image folder")
    f.add_argument("--backup", help="your Critical or Full backup of this phone (required)")
    f.add_argument("-s", "--serial")
    f.add_argument("--include", help="optional partitions to flash too, comma separated (e.g. oplusstanvbk,my_region)")
    f.add_argument("--wipe", action="store_true", help="factory reset (needed when changing region or OS)")
    f.add_argument("--no-reboot", action="store_true", help="stay in the bootloader when done")
    f.add_argument("--allow-fastbootd", action="store_true", help="allow fastbootd for logical partitions")
    f.add_argument("--accept-variant-change", action="store_true",
                   help="flash a ROM for another variant (region) of the same phone; you'll be asked to confirm")
    f.add_argument("--accept-unvalidated-arb", action="store_true",
                   help="override ONLY the 'Qualcomm ARB reader not validated' block (asks for confirmation)")
    f.add_argument("--dry-run", action="store_true", help="show the steps and stop")
    f.add_argument("--work", help="folder for the journal and extracted images (default: current folder)")
    f.add_argument("--resume", metavar="JOURNAL", help="continue an interrupted flash from its journal.json")
    f.add_argument("--no-restore-region", action="store_true",
                   help="on a region change, do NOT restore your own oplusstanvbk/my_region from the backup")
    f.set_defaults(fn=cmd_flash)

    rg = sub.add_parser("region", help="region / signal files: show them in a backup or restore them")
    rgsub = rg.add_subparsers(dest="region_command", required=True)
    rs = rgsub.add_parser("show", help="list the region files in a backup and check their hashes")
    rs.add_argument("--backup", required=True)
    rs.set_defaults(fn=cmd_region_show)
    rr = rgsub.add_parser("restore", help="write your backed-up region files back (e.g. after an OTA lost signal)")
    rr.add_argument("--backup", required=True)
    rr.add_argument("-s", "--serial")
    rr.add_argument("--only", help="comma separated subset, e.g. oplusstanvbk")
    rr.add_argument("--allow-fastbootd", action="store_true", help="needed to restore my_region (logical)")
    rr.add_argument("--no-reboot", action="store_true")
    rr.add_argument("--dry-run", action="store_true")
    rr.add_argument("--work", help="folder for the journal (default: current folder)")
    rr.set_defaults(fn=cmd_region_restore)

    ro = sub.add_parser("root", help="root assistant: patch init_boot/boot with Magisk, KernelSU or APatch")
    rosub = ro.add_subparsers(dest="root_command", required=True)
    rp = rosub.add_parser("prepare", help="extract the matching stock image and send it to the phone for patching")
    rp.add_argument("--manager", choices=("magisk", "kernelsu", "apatch"), required=True)
    src = rp.add_mutually_exclusive_group(required=True)
    src.add_argument("--rom", help="full OTA of the build the phone is running now")
    src.add_argument("--backup", help="a Full backup made on the current build")
    rp.add_argument("-s", "--serial")
    rp.add_argument("--work", help="folder for this root session (default: ./opx-root-<model>-<time>)")
    rp.set_defaults(fn=cmd_root_prepare)
    rpl = rosub.add_parser("pull", help="find the patched image on the phone, pull and check it")
    rpl.add_argument("--work", required=True)
    rpl.set_defaults(fn=cmd_root_pull)
    for name, fn, text in (("flash", cmd_root_flash, "flash the patched image (current slot)"),
                           ("unroot", cmd_root_unroot, "flash the stock image back (current slot)")):
        rf_ = rosub.add_parser(name, help=text)
        rf_.add_argument("--work", required=True)
        rf_.add_argument("--backup", help="your Critical or Full backup of this phone (required)")
        rf_.add_argument("-s", "--serial")
        rf_.add_argument("--no-reboot", action="store_true")
        rf_.add_argument("--dry-run", action="store_true")
        rf_.set_defaults(fn=fn)

    sub.add_parser("gui", help="open the graphical app").set_defaults(fn=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (bk.BackupError, ToolNotFound, RomError, pl.PayloadError, avb.AvbError, qcom.QcomError, OSError,
            RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
