# OPxFlasher

**Safety-first backup and flashing tool for OPPO / OnePlus (OPlus platform) phones.**
Windows and Linux, GUI and command line.

> Status: **Phase 1 – read-only backup.** Flashing is not implemented yet.

OPxFlasher was born after a OnePlus 13 (CN) got stuck on a ColorOS 17 release
candidate and needed an EDL reflash. The goal is a flasher that refuses to do
anything it cannot prove is safe, and that makes a verified backup of the
partitions you can never get back before anything else happens.

## Principles

- **Fail closed.** If a check can't confirm something is safe, the tool stops.
- **Read-only by design.** The backup module can only run a small allow-list of
  read-only commands on the phone (`getprop`, `ls`, `blockdev --getsize64`,
  `sha256sum`, `dd if=` with no `of=`). Anything else is rejected before it is sent.
- **Verify everything.** Each image is hashed on the PC while it downloads and
  compared with `sha256sum` run on the phone.
- **Warn about known-bad firmware**, e.g. ColorOS 17 CN builds where rebooting to
  fastboot has bricked phones.

## Phase 1: Backup

| Mode | What it reads |
|---|---|
| **Network essentials** | `oplusstanvbk_a/b`, `my_region` – the files that carry region / signal setup |
| **Critical** (default) | Network + `persist*`, `modemst1/2`, `fsg`, `fsc`, `ocdt`, `devinfo`, `oplusreserve*` – IMEI, calibration and device identity |
| **Full firmware** | Every physical partition except `userdata` (`super` includes all logical partitions) |

Backups need **root**: either a rooted system (Magisk / KernelSU / APatch, with root
granted to *Shell*) or a custom recovery with root ADB. Bootloader fastboot cannot
read partitions, so it is only used for information.

Each backup folder contains the `.img` files, a `manifest.json` (device, build,
slot, sizes, hashes, verification status) and a standard `SHA256SUMS` file.

## Install

Requires Python 3.10+.

```sh
git clone https://github.com/ox1d3x3/OPxFlasher
cd OPxFlasher
python -m pip install ".[gui]"
```

You also need Google's platform-tools (adb + fastboot). If they aren't on your PATH:

```sh
opx tools fetch     # downloads Google's official platform-tools (Google's terms apply)
opx tools where     # shows which adb/fastboot will be used
```

## Use

**GUI:** run `opxflasher` (or `opx gui`). Pick the phone, choose a mode, press
**Preview** to see exactly what will be read, then **Start backup**.

**Command line:**

```sh
opx devices                                  # list connected phones
opx info                                     # model, build, slot, root access, warnings
opx partitions --mode critical               # preview what a backup would read
opx backup --mode critical --out backups     # make the backup
opx backup --mode full --dry-run             # plan only, reads nothing
opx verify backups/<folder>                  # re-check a backup offline
```

## Roadmap

1. ✅ Backup + verify (read-only)
2. ROM detection, anti-rollback (ARB) checks, dry-run flash plans
3. Known-bad firmware warnings, flashing `super` from the bootloader to avoid broken fastbootd
4. Region conversion using **your own** backed-up signal files
5. Root assistant (patch `init_boot`, re-root after OTA)

Partitions that hold device identity (`persist`, `modemst*`, `fsg`, `ocdt`,
`devinfo`, `oplusreserve*`, `userdata`) are on a permanent **never-flash** list.

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest
```

The tests use a fake `adb` (`tests/fake_adb.py`) that serves partitions from a
folder, so the whole backup pipeline – including the GUI – runs without a phone.

## Credits and licence

See [CREDITS.md](CREDITS.md). OPxFlasher is free software under the
[GNU GPL v3.0 or later](LICENSE).

**Disclaimer:** modifying phone firmware can permanently damage your device.
You use this tool at your own risk.
