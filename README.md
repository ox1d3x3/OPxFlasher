# OPxFlasher

**Safety-first backup and flashing tool for OPPO / OnePlus (OPlus platform) phones.**
Windows and Linux, GUI and command line.

> Status: **Phase 1 (read-only backup) and Phase 2 (ROM check, dry run).** Flashing is not implemented yet.

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

## Phase 2: ROM check (dry run)

Point OPxFlasher at a firmware package and it tells you whether it is safe to
flash on **this** phone, and exactly what would be written where. Nothing is flashed.

- Reads **full OTA zips**, `payload.bin` and **folders of `.img` files**.
  Incremental OTAs are refused.
- Extracts any image from an OTA (hash-verified) – handy for `init_boot` when rooting.
- Compares the ROM with the **connected phone** (read-only) or a **backup folder**:

| Check | Blocks when |
|---|---|
| Package | the OTA is incremental, or can't be identified as full |
| Device match | the ROM's device codename differs from the phone's (a region change on the same device is a warning) |
| Known issue | the phone's current firmware is known to be dangerous (e.g. ColorOS 17 CN fastbootd) |
| AVB rollback | the ROM's `vbmeta` rollback index is lower than the phone's |
| Qualcomm ARB | the ROM's anti-rollback version is lower than the phone's, **or the reader isn't validated yet** |
| Sizes | an image is bigger than its partition, or the dynamic groups don't fit in `super` |

`persist`, `modemst*`, `fsg`, `ocdt`, `devinfo`, `oplusreserve*` and `userdata` are
always skipped. `oplusstanvbk`, `my_region` and `odm` are marked **optional** (region /
signal files), to be restored from your own backup.

> **Qualcomm ARB is experimental.** The reader has not been validated against real
> OnePlus 13 firmware yet, so for now it always blocks and shows the raw numbers.
> Use `opx rom arb <image>` to compare them with values the community already knows;
> once confirmed, the profile is switched on.

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

opx rom inspect ROM.zip                      # device, build, full/incremental, images
opx rom extract ROM.zip init_boot vbmeta     # pull images out (hash-verified)
opx rom avb vbmeta.img                       # AVB rollback index and descriptors
opx rom arb xbl_config.img                   # EXPERIMENTAL Qualcomm anti-rollback
opx plan ROM.zip                             # check against the connected phone
opx plan ROM.zip --backup backups/<folder>   # ...or against a backup
opx plan ROM.zip --json                      # machine-readable report
```

Some newer OTAs compress with zstd; install the extra with `pip install ".[zstd]"`.

## Roadmap

1. ✅ Backup + verify (read-only)
2. ✅ ROM detection, anti-rollback checks, dry-run flash plans (Qualcomm ARB pending validation)
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
folder, and `tests/romfactory.py` builds synthetic OTAs, vbmeta images and signed
ELFs, so backup, ROM checks and the GUI all run without a phone or real firmware.

## Credits and licence

See [CREDITS.md](CREDITS.md). OPxFlasher is free software under the
[GNU GPL v3.0 or later](LICENSE).

**Disclaimer:** modifying phone firmware can permanently damage your device.
You use this tool at your own risk.
