<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/wordmark-on-dark.svg">
    <img src="assets/brand/wordmark-on-light.svg" alt="OPxFlasher" width="420">
  </picture>
</h1>

<p align="center">
  <b>AIO tool for OnePlus / OPPO devices.</b><br>
  Backup, ROM check, flashing, signal restore and root. Windows, Linux and macOS; GUI and command line.
</p>

<p align="center">
  <a href="https://github.com/ox1d3x3/OPxFlasher/actions/workflows/ci.yml"><img src="https://github.com/ox1d3x3/OPxFlasher/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

> Status: **all five phases** – backup, ROM check, guarded flashing, region conversion / signal restore, root assistant.
> Tested against a simulated phone only so far. Treat flashing as experimental until it has been run on real hardware.

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
| **Full firmware** | The whole Android OS: every physical partition (`super` includes all logical partitions), **none of your personal data** |

Your personal data is never backed up, in any mode: `userdata` (apps, photos,
files), `metadata` (its encryption data) and `frp` (Google account lock) are always
skipped. This is built into the code, so a device profile can't change it.

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
| Device match | the ROM's device codename differs from the phone's (e.g. an OnePlus 13 ROM on an OnePlus 15) |
| Variant | the ROM is for another variant of the same phone (e.g. CN PJZ110 vs EU CPH2653). You can go ahead by confirming it: tick *Yes, I want to flash this other variant* in the app (it starts unticked), or `--accept-variant-change` and typing CHANGE VARIANT on the command line. It's a region change, so a data wipe is required and your own signal files are restored |
| Known issue | the phone's current firmware is known to be dangerous (e.g. ColorOS 17 CN fastbootd) |
| AVB rollback | the ROM's `vbmeta` rollback index is lower than the phone's |
| Qualcomm ARB | the ROM's anti-rollback version is lower than the phone's, **or the reader isn't validated yet** |
| Sizes | an image is bigger than its partition, or the dynamic groups don't fit in `super` |

`persist`, `modemst*`, `fsg`, `ocdt`, `devinfo`, `oplusreserve*`, `userdata`,
`metadata` and `frp` are always skipped. `oplusstanvbk`, `my_region` and `odm` are marked **optional** (region /
signal files), to be restored from your own backup.

> **Qualcomm ARB is experimental.** The reader has not been validated against real
> OnePlus 13 firmware yet, so for now it always blocks and shows the raw numbers.
> Use `opx rom arb <image>` to compare them with values the community already knows;
> once confirmed, the profile is switched on.

## Phase 3: Guarded flashing

Flashing only starts when **every** gate passes:

1. The ROM check has no blocking result. The single exception is the *unvalidated*
   Qualcomm ARB reader, which you can override only by typing `ARB CHECKED` after
   checking the numbers yourself. A lower ARB, a lower AVB index, a different
   device or an incremental OTA can never be overridden.
2. A verified **Critical or Full backup of this phone** is selected (same serial and model).
3. The phone's current firmware is not known to break the reboot that is needed.
   On ColorOS 17 CN, OPxFlasher refuses to reboot to the bootloader or fastbootd at all.
4. You type `FLASH`.

How it flashes:

- Images are extracted and hash-checked **before** the phone is rebooted.
- Physical partitions go to **both slots** from the bootloader; `vbmeta` is written last.
- Logical partitions come from a `super.img` (flashed from the bootloader, no fastbootd),
  or through fastbootd only if you allow it and the firmware isn't flagged.
- Never-flash partitions are refused again at the moment of writing.
- Every step is saved to `journal.json`. The run stops at the first error, and
  `opx flash --resume journal.json` continues without repeating finished steps.
  **Stop** waits for the current step; a running write is never interrupted.
- Optional: factory reset (`--wipe`), stay in bootloader (`--no-reboot`),
  flash the ROM's region files (`--include oplusstanvbk,my_region`, usually off).

## Phase 4: Region conversion and signal restore

Converting a phone to another region (for example a Chinese OnePlus 13 to an
Indian OxygenOS build) usually breaks mobile signal, because the new firmware's
region files don't match the phone's modem setup. OxygenOS updates can break it
again later by replacing those files.

**Conversion mode.** When the ROM is for a different region/variant of the same
device, OPxFlasher:

- requires a data wipe (the phone wouldn't boot with the old data),
- flashes the new firmware, then writes the phone's **own** `oplusstanvbk`
  (both slots) and `my_region` back **from your backup**, so the modem keeps the
  setup that works with your SIM.

**Signal restore on its own** (no ROM), for after an update took the signal away:
the *Signal / region* tab or `opx region restore`.

Rules that always apply:

- Only region files (`oplusstanvbk`, `my_region`, `odm`) can be restored. Identity
  partitions (`persist`, `modemst*`, `fsg`, `ocdt` ...) never are.
- The backup must be from this same phone, and **every image is re-hashed against
  its manifest before anything is written**. One mismatch and nothing is touched.
- `my_region` lives inside `super`, so restoring it needs fastbootd (which is refused
  on firmware known to break it).
- A file can't come from both the ROM and your backup in the same run.

## Phase 5: Root assistant

Patching is done by your root app on the phone (Magisk, KernelSU or APatch), as
those projects recommend. OPxFlasher handles the error-prone parts around it:

1. **Stock image that matches the running build.** It's taken from a full OTA
   whose build fingerprint must equal the phone's, or from a Full backup made on
   the same build. A mismatched `init_boot` is the classic bootloop, so anything
   else is refused. Magisk and KernelSU use `init_boot`; APatch uses `boot`.
2. **Sent to the phone** as `Download/opx_stock_<partition>.img` for you to patch.
3. **Found, pulled and checked**: a valid boot image, same header version as stock,
   and actually different from stock.
4. **Flashed to the current slot only**, behind the usual gates (verified backup,
   known-bad firmware block, typed `ROOT`). The image is re-hashed right before writing.
5. **Unroot** writes the stock image back the same way (typed `UNROOT`).

After an OTA, run the same steps with the new build's full OTA.

## Install

### Windows and Linux: one file, nothing to install

Download from the [latest release](https://github.com/ox1d3x3/OPxFlasher/releases/latest):

- **Windows:** `OPxFlasher-<version>-windows-x64.exe`. Double-click to run. The file
  isn't code-signed yet, so Windows SmartScreen may say "Windows protected your PC":
  choose *More info → Run anyway*.
- **Linux:** `OPxFlasher-<version>-linux-x86_64.AppImage`. Make it executable once
  (`chmod +x OPxFlasher-*.AppImage`, or tick "Allow executing" in file properties),
  then double-click it. Works on most distributions from 2022 onwards. If it says FUSE
  is missing, install `libfuse2` or run it with `--appimage-extract-and-run`.

The command-line tool `opx` isn't in these files; install with pip (below) for that.

Every update is published as a **release candidate** (`v0.7.0-rc.1`, `-rc.2`, ...),
marked pre-release on GitHub; the app's title shows the exact build. Final releases
are published when a version is ready.

### macOS (and any system with Python)

Requires Python 3.10+ and [pipx](https://pipx.pypa.io) (`brew install pipx`):

```sh
pipx install "opxflasher[gui,zstd] @ git+https://github.com/ox1d3x3/OPxFlasher"
opxflasher        # the app
opx --help        # the command line
```

Update later with `pipx upgrade opxflasher`. On Linux and Windows the same command works
if you prefer it to the portable files.

From a checkout instead: `python -m pip install ".[gui,zstd]"`.

### adb and fastboot

OPxFlasher doesn't ship Google's platform-tools (adb + fastboot). If they aren't on your
PATH, download them once:

```sh
opx tools fetch     # downloads Google's official platform-tools (Google's terms apply)
opx tools where     # shows which adb/fastboot will be used
```

With the portable files you can also put Google's `platform-tools` folder next to the
`.exe` / `.AppImage`. On macOS, `brew install android-platform-tools` works too.

## Restore the whole ROM from a backup

A **Full firmware** backup is an offline copy of your phone's whole Android OS (never your
personal data). If the ROM breaks, put it back:

- **App:** Backup tab → **Restore full ROM...** → choose the backup folder.
- **Command line:** `opx restore-rom <backup-folder>` (add `--dry-run` to only see the checks).

The backup is checked like any ROM (phone, variant, known bad firmware, anti-rollback,
sizes), then written from the bootloader: firmware and boot images of the slot that was
active at backup time, then `super` (system, vendor, my_region, ...), then vbmeta.
Identity partitions and your data are never written.

- Only restores to **the phone it came from** (serial must match).
- Going back to an **older build** than the one installed needs a data wipe.
- If the phone **can't boot**, put it in fastboot. OPxFlasher then can't read the phone's
  current firmware, so you confirm it hasn't installed an update since the backup
  (`--not-updated`). Restoring older firmware over a newer anti-rollback version can
  hard-brick a phone.

## Reboot and flash one image

**Reboot buttons** (phone panel) and `opx reboot system|bootloader|fastbootd|recovery`
work from Android, recovery, the bootloader or fastbootd. On firmware known to brick on
those reboots (ColorOS 17 CN: fastbootd and the bootloader) the buttons are disabled with
the reason. From the bootloader OPxFlasher can't see the build, so Fastbootd asks first.

**Flash image** tab and `opx flash-image <partition> <file.img> --backup <folder>`
flash a single image, such as a recovery, a (patched) boot or init_boot, vendor_boot,
dtbo or vbmeta, to the current slot, the other slot or both.
It refuses partitions holding the phone's identity or your data, partitions inside
`super` (use a full ROM flash), files of the wrong type (e.g. a vbmeta into boot), images
bigger than the partition, and firmware with a broken bootloader. `--disable-verity`
(vbmeta only) flashes with verity and verification off. As for every flash, a Critical or
Full backup of this phone is required, the image is re-hashed right before writing, and
you confirm by typing FLASH. If the phone is already in fastboot, the backup tells
OPxFlasher which phone it is.

## Official ROMs

OPxFlasher can find and download official OTA packages for your phone, using the
public [OTA catalog by Daniel Springer](https://roms.danielspringer.at) (thank you!).
Packages come straight from OPPO / OnePlus servers; OPxFlasher checks the size and MD5
(when the catalog has them) and that it's a real zip, then runs the usual ROM check.

- **App:** ROM check tab → **Official ROMs...** (connect the phone first). ROMs for your
  own model are listed first; other variants (regions) are marked and ask before
  downloading, because flashing them changes the region and needs a data wipe.
- **Command line:**
  ```sh
  opx ota list                      # ROMs for the connected phone
  opx ota list --model PJZ110       # or any model number
  opx ota download --id <id> --out ~/Downloads
  ```

Catalog answers are cached, and OPxFlasher only asks the catalog when you open it.

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

opx flash ROM.zip --backup backups/<folder> --allow-fastbootd --dry-run   # show the steps
opx flash ROM.zip --backup backups/<folder> --allow-fastbootd             # flash (asks you to type FLASH)
opx flash --resume opx-flash-<time>/journal.json                          # continue an interrupted flash

opx region show --backup backups/<folder>                       # region files in a backup, hash-checked
opx region restore --backup backups/<folder> --allow-fastbootd  # restore signal files (asks you to type RESTORE)
```

```sh
opx root prepare --manager magisk --rom FULL_OTA_OF_CURRENT_BUILD.zip    # check + send stock init_boot to the phone
opx root pull --work opx-root-<model>-<time>                             # after patching in the app
opx root flash --work opx-root-<model>-<time> --backup backups/<folder>  # asks you to type ROOT
opx root unroot --work opx-root-<model>-<time> --backup backups/<folder> # asks you to type UNROOT
```

On a region change, `opx flash` restores your own region files automatically
(`--no-restore-region` turns that off) and refuses to run without `--wipe`.

Some newer OTAs compress with zstd; install the extra with `pip install ".[zstd]"`.

## Roadmap

1. ✅ Backup + verify (read-only)
2. ✅ ROM detection, anti-rollback checks, dry-run flash plans (Qualcomm ARB pending validation)
3. ✅ Guarded flashing: known-bad firmware blocks, `super.img` from the bootloader, journal and resume
4. ✅ Region conversion and signal restore using **your own** backed-up region files
5. ✅ Root assistant: matching stock image, patch in the app, checked flash to the current slot, unroot

Partitions that hold device identity (`persist`, `modemst*`, `fsg`, `ocdt`,
`devinfo`, `oplusreserve*`) or your personal data (`userdata`, `metadata`, `frp`) are
on a permanent **never-flash** list. A factory reset still erases `userdata` and
`metadata` when you choose it.

## Development

```sh
python -m pip install -e ".[dev]"
python -m pytest
```

The tests use a fake `adb` (`tests/fake_adb.py`) that serves partitions from a
folder, and `tests/romfactory.py` builds synthetic OTAs, vbmeta images and signed
ELFs, so backup, ROM checks and the GUI all run without a phone or real firmware.
`tests/fake_fastboot.py` simulates the bootloader and fastbootd (including a
firmware where rebooting to fastbootd bricks the phone), so every flash path,
failure and resume is tested end to end.

## Credits and licence

See [CREDITS.md](CREDITS.md). OPxFlasher is free software under the
[GNU GPL v3.0 or later](LICENSE).

**Disclaimer:** modifying phone firmware can permanently damage your device.
You use this tool at your own risk.
