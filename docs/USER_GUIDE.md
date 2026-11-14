# OPxFlasher user guide

Everything OPxFlasher does, in the order you'd usually use it. For installing, see the
[README](../README.md#download); for every command, see the [command-line reference](CLI.md).

1. [How OPxFlasher keeps your phone safe](#how-opxflasher-keeps-your-phone-safe)
2. [Backup](#backup)
3. [ROM check (dry run)](#rom-check-dry-run)
4. [Flashing a ROM](#flashing-a-rom)
5. [Restore the whole ROM from a backup](#restore-the-whole-rom-from-a-backup)
6. [Region conversion and signal restore](#region-conversion-and-signal-restore)
7. [Root assistant](#root-assistant)
8. [Reboot and flash one image](#reboot-and-flash-one-image)
9. [Official ROMs](#official-roms)
    - [Convert to a full ROM](#convert-to-a-full-rom)
10. [Phone won't start?](#phone-wont-start)
11. [Apps (debloat)](#apps-debloat)
12. [Diagnostics](#diagnostics)
13. [Everyday helpers](#everyday-helpers)

## How OPxFlasher keeps your phone safe

- **Fail closed.** If a check can't confirm something is safe, the tool stops.
- **Read-only by design.** The backup module can only run a small allow-list of
  read-only commands on the phone (`getprop`, `ls`, `blockdev --getsize64`,
  `sha256sum`, `dd if=` with no `of=`). Anything else is rejected before it is sent.
- **Verify everything.** Each image is hashed on the PC while it downloads and
  compared with `sha256sum` run on the phone.
- **Warn about known-bad firmware**, e.g. ColorOS 17 CN builds where rebooting to
  fastboot has bricked phones.

## Backup

| Mode | What it reads |
|---|---|
| **Network essentials** | `oplusstanvbk_a/b`, `my_region` – the files that carry region / signal setup |
| **Critical** (default) | Network + `persist*`, `modemst1/2`, `fsg`, `fsc`, `ocdt`, `devinfo`, `oplusreserve*` – IMEI, calibration and device identity |
| **Full firmware** | The whole Android OS: every physical partition (`super` includes all logical partitions), **none of your personal data** |

Empty space isn't stored: runs of zeros (a lot of `super`) are left as holes in the image
files, so a Full backup takes far less disk than the phone's partitions add up to, while
every file still reads back byte-for-byte (and hashes) the same. A Full backup is refused
while an OTA update is still being applied or merged, because `super` is half old, half new
until it finishes.

Your personal data is never backed up, in any mode: `userdata` (apps, photos,
files), `metadata` (its encryption data) and `frp` (Google account lock) are always
skipped. This is built into the code, so a device profile can't change it.

Backups need **root**: either a rooted system (Magisk / KernelSU / APatch, with root
granted to *Shell*) or a custom recovery with root ADB. Bootloader fastboot cannot
read partitions, so it is only used for information.

Each backup folder contains the `.img` files, a `manifest.json` (device, build,
slot, sizes, hashes, verification status) and a standard `SHA256SUMS` file.

## ROM check (dry run)

Point OPxFlasher at a firmware package and it tells you whether it is safe to
flash on **this** phone, and exactly what would be written where. Nothing is flashed.

- Reads **full OTA zips**, `payload.bin` and **folders of `.img` files**.
  A folder of loose images (for example the `OTA_FILES_HERE` folder of a fastboot "flasher"
  kit) has no OTA metadata, so OPxFlasher reads the phone model and build from the images' signed
  AVB data (`vbmeta_system`, `vbmeta_vendor`, `vbmeta`) instead. If those disagree about the device,
  or there's no such data, the device can't be confirmed and the check blocks. A flasher kit's
  *zip* can't be checked directly: unzip it and choose its images folder.
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
| Rollback fuse | OnePlus 13 / 13T / 15 on ColorOS 16.0.3.501 or later: the ROM (or backup) is **older than the installed build**, or its age can't be told. Installing an older build there blows a hardware fuse that nothing repairs, so this can't be overridden |
| Sizes | an image is bigger than its partition, or the dynamic groups don't fit in `super` |

`persist`, `modemst*`, `fsg`, `ocdt`, `devinfo`, `oplusreserve*`, `userdata`,
`metadata` and `frp` are always skipped. `oplusstanvbk`, `my_region` and `odm` are marked **optional** (region /
signal files), to be restored from your own backup.

> **Qualcomm ARB is experimental.** The reader has not been validated against real
> OnePlus 13 firmware yet, so for now it always blocks and shows the raw numbers.
> Use `opx rom arb <image>` to compare them with values the community already knows;
> once confirmed, the profile is switched on.

## Flashing a ROM

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

### Restore without a PC: recovery zip

OPxFlasher can turn a Full firmware backup into a zip you flash in recovery, for
[AERA Recovery](https://aera-recovery.com) (OnePlus 13: *dodge*) or for OrangeFox / TWRP:

- **App:** Backup tab → **Make recovery zip...** → pick the recovery → choose the backup.
- **Command line:** `opx restore-zip <backup-folder> --out <folder> [--recovery aera|orangefox]`
  (`--compress` for a smaller file).

**OrangeFox / TWRP zip:** a normal flashable zip with its own installer. Before writing anything
it checks the phone's board, **serial number** and active slot match the backup, that no system
update is half-applied, and that the bootloader firmware (`xbl_config`, `abl`) hasn't changed
since the backup, so an older backup can't trip anti-rollback. Then it verifies every file in the
zip, writes the firmware images to the current slot and the whole backed-up `super` partition, and
reads it all back. If a check fails, nothing is written.

**AERA zip:** uses the standard image-zip layout AERA installs natively: `android-info.txt`
(phone board), the backup's firmware and boot images, one image per partition inside
`super` (system, vendor, my_region, ...) and `super_empty.img`. AERA refuses it on another
phone model, checks every image, rebuilds only the current slot inside `super`, and compares
anti-rollback with the phone before writing.

**It's only for the phone the backup came from.** AERA can't check the serial number, so the
zip's name includes it. Identity partitions and your data are never in the zip.

## Region conversion and signal restore

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

## Root assistant

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

How the download works:

- **The way the phone's updater asks.** OPPO's servers refuse downloads from anything
  that isn't the system updater, so OPxFlasher sends the updater's identity (a `Dalvik`
  User-Agent with your phone's model and Android version, and the updater's `userId`)
  and follows the server's redirect to the package. No browser and no extra downloader
  needed.
- **Fast and resumable.** Big packages download over 4 connections at once. A dropped
  connection is retried, and an expired download link (they last about an hour) is renewed
  automatically. **Cancel** pauses: press **Resume download** later, even after closing the
  app, and it carries on from where it stopped (the partial file is `<name>.zip.part`).
- **Checked.** The MD5 comes from the catalog, or from the server when the catalog has none.
  A package that fails the MD5 or isn't a complete zip is deleted.
- **Logged.** Every step goes into the app's log, so **Export log...** shows what happened.

**If the catalog goes away.** **Tools → Save ROM catalog snapshot...** keeps the whole list (every
phone, region and build with its official link, size and MD5) in one JSON file; save one now and
then. When the catalog can't be reached, OPxFlasher tries the mirror at `ota.ox1de.xyz`, then any
mirrors you added (**Tools → ROM catalog mirrors...**: add, test or remove a server that speaks the
same API, e.g. your own copy of opx-ota-mirror), then the last list it downloaded, then your snapshot (**Tools → Use a ROM catalog snapshot...** picks a
different one). Downloads always come straight from OPPO / OnePlus servers; the snapshot only
holds the links. Command line: `opx ota snapshot --out <folder>`, `opx ota mirrors --add <host>`.

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

## Convert to a full ROM

**ROM check → Convert to full ROM...** (or `opx rom convert`) turns a ROM into a package you can
install from a custom recovery, keeping your data:

- **Input:** a full OTA zip (for example one from Official ROMs), a `payload.bin`, a folder of
  images, a fastboot flasher-kit zip, or an image set with `super.img` (also sparse or split into
  pieces). Incremental OTAs can't be converted: they only hold the changes between two builds.
- **First it tells you what the ROM is:** type, phone and model, build, Android version, security
  patch, how many images (firmware vs. inside super), anything a full ROM normally has that's
  missing, and which partitions are never written (identity and personal data are always left out).
- **Recovery zip** (`<name>-fullrom.zip`) for **OrangeFox, TWRP and AERA**. Copy it to the phone and
  flash it. Before writing anything the installer checks:
  - it's the phone the ROM is for (its device codename; the chip platform name, shared by many
    phones, never counts), and the **same variant** (it never changes region, which would need a
    data wipe). The installed model and build are read from `my_manifest`, `odm`, `vendor`,
    `product`, `system_ext` and `system`,
  - the ROM is **not older than the installed Android** (the newest build date of the installed
    partitions; reinstalling the exact installed build is fine): installing older software can blow
    the OnePlus 13 / 13T / 15 anti-rollback fuse,
  - no system update is half-applied,
  - every partition exists and is big enough, and `lptools` is available (for system, vendor, odm...),
  - **every file in the zip** matches its SHA-256.

  Then it writes the current slot (firmware, boot images, the logical partitions resized inside
  super, `vbmeta` last) and reads everything back. Images over 1 GiB are stored in parts, because
  some recoveries can't unzip bigger files.
- **Image folder** (`<name>-fullrom/`): plain images plus `opxflasher-rom.json`, for fastboot or
  OPxFlasher. **Use in ROM check** loads the result, so OPxFlasher checks it against your phone
  before flashing, with all the usual safety checks.
- A ROM that doesn't say which phone it's for, or how new it is (some image kits), only gets an
  image folder: a recovery couldn't check it safely. Image kits often only carry the chip platform's
  build (for example `qti/canoe/canoe`), which many phones share, so that never counts as the phone.

AERA's own "image zip" format isn't used for converted ROMs, because it can't check for a
downgrade. AERA installs the recovery zip like any flashable zip.

## Phone won't start?

**Phone won't start?** (phone panel, or `opx rescue`) looks at what's connected and works out
the phone's state: fastboot, fastbootd, recovery, Android stuck while starting, ADB not
allowed, Qualcomm 9008 (EDL), or nothing at all. It then lists what to try, in order, each
with a button: restart, recovery, restore the full ROM from your backup, make a recovery zip,
download the official ROM, save logs, USB check.

**9008 (EDL) identify** (`opx edl-info`): a phone stuck in 9008 is only running the chip's
emergency loader. OPxFlasher can read its chip serial, hardware ID, OEM key hash and boot ROM
version over Qualcomm's Sahara protocol. That's read-only: no loader is sent and nothing is
written. EDL *flashing* isn't included. On OnePlus / OPPO it needs OnePlus's signed loader
and their authorisation, which only authorised service centres have. Windows needs the
"Qualcomm HS-USB QDLoader 9008" driver for this; on Linux add yourself to the `dialout` group.

## Apps (debloat)

The **Apps** tab (or `opx apps`) removes system apps you don't use, **for your user only**:
nothing on the system partition changes, **Restore** puts an app back at any time, and a
factory reset brings everything back. No root needed. Apps Android can't run without
(System UI, Settings, the phone app, the package installer, ...) are locked, and a device
profile can lock more (`keep_apps`).

- Remove or restore selected apps; **Restore all I removed** undoes everything OPxFlasher removed on this phone.
- Share lists: **Save my list...** writes what you removed (one package per line); **Remove from list...** applies someone else's list, skipping anything locked or not on your phone.
- Command line: `opx apps list [--removed] [--search X]`, `opx apps remove <pkg>... | --from list.txt`,
  `opx apps restore <pkg>... | --all`, `opx apps export list.txt`.

## Diagnostics

- **Save phone logs...** (`opx logs`): Android log, kernel log and the log of the last crash or
  unexpected reboot (`/sys/fs/pstore`), plus the phone's properties, in one zip for a bug report.
  Serial numbers are hidden. Kernel and crash logs need root.
- **Known problems:** after saving, and with **Read a logs zip...** (`opx logs --read <zip>`) for
  logs saved earlier, OPxFlasher looks for well-known messages and says what they usually mean:
  kernel panic, verified boot / dm-verity rejecting a partition, system partitions or `/data`
  that won't mount, zygote or system_server crash loops, an unfinished update, a modem crash,
  and the last restart reason. They're hints, not a diagnosis; `findings.txt` in the zip has
  the matching lines.
- **List partitions** (`opx partitions --all`, `--csv file`): every partition with its size, slot
  and role (running slot, other slot, inside super, phone identity, your data). Read-only, needs root.

## Everyday helpers

- **Connection light** (top of the phone panel): **green** *Connected* with the mode (Android,
  recovery, bootloader, fastbootd), **amber** *Waiting* (allow USB debugging on the phone, or it's
  offline), **red** *Disconnected* (no phone, or adb / fastboot missing). OPxFlasher checks every few
  seconds and refreshes the phone details by itself when you plug a phone in or unplug it. It pauses
  while a backup, flash or other job runs, or while a dialog is open.
- **Menus:** *File* (backups folder, verify a backup, export log, quit), *Phone* (refresh with F5,
  reboot to Android / bootloader / fastbootd / recovery, phone not showing up, phone won't start),
  *Tools* (download adb and fastboot, official ROMs, read a logs zip, language) and *Help* (user
  guide with F1, command-line reference, report a problem, check for updates, releases, About).
- **About** (*Help → About OPxFlasher*, or `opx about`): version and build, developer, project
  links, support links, licence and credits. **Copy details** copies the version and system details
  for a bug report.
- **Tool check at start-up:** OPxFlasher looks for adb and fastboot every time it starts. If they're
  missing it asks to download Google's official platform-tools (about 10 MB, saved for your user
  only). **Download** fetches them with a progress bar and then looks for your phone; **Decline**
  opens the app with a *Tools not downloaded* banner and a **Click here to download** link.
- **Phone panel:** model and variant (e.g. *OnePlus 13 China (PJZ110)*), build, Android,
  security patch, active slot, bootloader locked/unlocked, root access and this phone's
  **last backup** (or a reminder to make one before flashing).
- **Export log...** (under the tabs): saves the log, app/OS versions, phone details and the
  last flash journals as a zip for a GitHub issue. Serial numbers are hidden.
- **Check for updates:** asks GitHub for newer releases (including release candidates) only
  when you click it. OPxFlasher sends nothing on its own.
- **Phone not showing up?** (phone panel, or `opx usb-check`): on Windows it finds a
  OnePlus / OPPO phone with a missing or broken USB driver, tells you whether it's the adb or the
  fastboot driver, and explains two fixes: Windows Update's optional driver updates (which often
  include the Android driver), or the Google USB driver (**Get the driver** opens Google's official
  page) applied in Device Manager. On Linux it checks your user may open the phone (udev rules /
  `plugdev`). It also spots a phone stuck in Qualcomm 9008 (EDL) mode. OPxFlasher never installs
  drivers, opens Windows settings or asks for administrator rights itself; only the driver install
  asks for approval.
