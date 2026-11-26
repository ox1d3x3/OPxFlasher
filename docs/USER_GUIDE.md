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
- Compares the ROM with the **connected phone** (read-only) or a **backup folder**. With neither,
  it only checks the ROM itself (type, which phone it's for, its anti-rollback indexes) and asks you
  to connect the phone; nothing about the phone is guessed. Problems are listed first, then what
  passed, then notes.
- A phone **without root** is compared with what Android reports about it (model, variant, build,
  hardware project). Its partitions can't be read: the bootloader checks each image fits before
  writing, and the Qualcomm anti-rollback numbers are your decision (see *Anti-rollback you confirm*).
- A phone already in **fastboot / fastbootd / recovery** is compared with what it reported the last
  time OPxFlasher saw it in Android (same serial). Never seen in Android (or its Android doesn't
  start): it's known from the bootloader (e.g. `dodge` = OnePlus 13), so a ROM for another phone is
  still refused; the installed build and region can't be read, so you confirm "same or newer build"
  in the anti-rollback warning, and tick Wipe data yourself if the region changes.
- **Tabs:** *ROMs* gets, verifies and converts ROMs (nothing touches the phone); *Flash ROM* is
  just: choose the ROM → **Check ROM** → **Flash...**.
- **A backup is recommended, not required.** The Flash dialog asks for one; without it, tick *Flash
  without a backup: I agree with the risk* (CLI: `opx flash --no-backup`, type `NO BACKUP`). Every other
  check stays: wrong phone, older build (fuse), region change (wipe + confirmation), ARB, identity
  partitions never written.
- **Mobile network files** (`oplusstanvbk`, `my_region`) aren't written by a same-region flash: the
  phone keeps its own. If the phone was converted from another region earlier, tick *This phone was
  converted...* to write your own ones from the backup you made before converting (Network
  essentials or Critical).

| Check | Blocks when |
|---|---|
| Package | the OTA is incremental, or can't be identified as full |
| Device match | the ROM's device codename differs from the phone's (e.g. an OnePlus 13 ROM on an OnePlus 15) |
| Variant | the ROM is for another variant of the same phone (e.g. CN PJZ110 vs Global CPH2653). You can go ahead by confirming it: tick *Yes, I want to flash this other variant* in the app (it starts unticked), or `--accept-variant-change` and typing CHANGE VARIANT on the command line. It's a region change, so a data wipe is required and your own signal files are restored |
| Known issue | the phone's current firmware is known to be dangerous (e.g. ColorOS 17 CN fastbootd) |
| Project | never blocks; warns when the ROM is built for another OPPO hardware project than the phone's (see *Cross-flashed phones* below) |
| Root | the connected phone has no root, so its partitions can't be compared |
| AVB rollback | the ROM's `vbmeta` rollback index is lower than the phone's |
| Qualcomm ARB | the ROM's anti-rollback version is lower than the phone's, **or the reader isn't validated yet** |
| Rollback fuse | OnePlus 13 / 13T / 15 on ColorOS 16.0.3.501 or later: the ROM (or backup) is **older than the installed build**, or its age can't be told. Installing an older build there blows a hardware fuse that nothing repairs, so this can't be overridden |
| Sizes | an image is bigger than its partition, or the dynamic groups don't fit in `super` |

`persist`, `modemst*`, `fsg`, `ocdt`, `devinfo`, `oplusreserve*`, `userdata`,
`metadata` and `frp` are always skipped. `oplusstanvbk` and `my_region` are the region / signal files:
they're **optional** (left as the phone has them, or restored from your own backup), unless the
ROM's vbmeta checks them at boot. Then they're always written from the ROM, because the phone's old
copy would fail verification with the new vbmeta. `odm` is build content and is always written.

### Cross-flashed phones (another hardware project)

OPPO and OnePlus number their hardware: the phone's board reports its **project**
(`ro.boot.prjname`, e.g. 24821), and a full OTA lists the projects it's built for
(`oplus_project_numbers`, e.g. 24875). Some shops convert one phone into another, for example an
OnePlus 13T (project 24821) running OnePlus 13s India firmware (CPH2723, project 24875). The
model and build then match the ROM, but the board doesn't.

On such a phone the phone's **own updater refuses** packages for another project: *Software
update* finds nothing, and *Local install* fails after a few seconds without installing anything.
OPxFlasher shows the ROM's project (in *Verify ROM* and next to the selected package) and the ROM
check warns when it differs from the phone's, with the NV id (region / carrier) of both.
OPxFlasher doesn't get around the phone's updater. Flashing from the bootloader (fastboot) doesn't
go through the updater, so this check isn't made there: OPxFlasher can flash a full ROM for the
phone's model that way, with root for the backup first and an unlocked bootloader (unlocking erases
the phone). **Don't relock the bootloader on a cross-flashed phone**: with any partition that
doesn't match what the bootloader expects, a locked phone won't start.

> **Qualcomm ARB is experimental.** The reader has not been validated against real
> OnePlus 13 firmware yet, so for now it always blocks and shows the raw numbers.
> Use `opx rom arb <image>` to compare them with values the community already knows;
> once confirmed, the profile is switched on.

## Verify a ROM

**ROMs → Verify ROM...** (also in the ROMs menu, or `opx rom verify <rom>`) checks any ROM
before you use it. No phone needed, nothing is written.

- **Integrity, every byte:** a full OTA's `payload.bin` against the SHA-256 in
  `payload_properties.txt` (or every data block against its own hash), an OPxFlasher full ROM
  against its recorded SHA-256 list, every zip entry against its CRC, sparse images chunk by chunk,
  and boot-type images (boot, init_boot, vendor_boot, dtbo, recovery) against the digests in the
  ROM's own `vbmeta`. For loose images (folders, flasher kits) the system-type images (system,
  vendor, product, odm, my_* ...) are also checked the way the phone does at boot: their dm-verity
  hash tree is rebuilt and its root compared with `vbmeta` (or the image's own AVB footer). A
  patched or damaged image fails. `--quick` (or unticking *Full check*) skips reading the data.
- **Complete:** the images a full ROM always has, and every partition the ROM's `vbmeta` chain
  covers. Logical images must be at least as big as the data `vbmeta` protects.
- **The right files:** boot, vendor_boot, vbmeta and dtbo must have their real headers; Qualcomm
  firmware must be signed ELF files.
- **Who can install it:** *OPxFlasher* (ROM check, then Flash), and *OrangeFox / AERA* (ready
  when it's an OPxFlasher recovery zip; otherwise after converting). When converting would make it
  usable, OPxFlasher lists the files it found and offers to convert it.

## Flashing a ROM

Flashing only starts when **every** gate passes:

1. The ROM check has no blocking result. The single exception is the *unvalidated*
   Qualcomm ARB reader, which you can override only through the anti-rollback warning (or `ARB CHECKED` in the CLI) after
   checking the numbers yourself. A lower ARB, a lower AVB index, a different
   device or an incremental OTA can never be overridden.
2. A verified **Critical or Full backup of this phone** is selected (same serial; the model may differ only
   when the phone was converted to another region of the same phone).
3. The phone's current firmware is not known to break the reboot that is needed.
   On ColorOS 17 CN, OPxFlasher refuses to reboot to the bootloader or fastbootd at all.
4. You type `FLASH`.

How it flashes:

- Images are extracted and hash-checked **before** the phone is rebooted.
- Physical partitions go to **both slots** from the bootloader; `vbmeta` is written last.
- Logical partitions come from a `super.img` (flashed from the bootloader, no fastbootd),
  or through fastbootd only if you allow it and the firmware isn't flagged.
- A factory reset (`--wipe`, required for a region conversion) runs **first**, right after the
  bootloader check, while the bootloader is still the phone's own. With fastbootd, the flash ends
  there with a plain reboot instead of going back to the bootloader: a ColorOS / OxygenOS 17
  bootloader is reported to leave fastboot mode within a second on Chinese OnePlus 13 hardware.
- Never-flash partitions are refused again at the moment of writing.
- Every step is saved to `journal.json`. The run stops at the first error, and
  `opx flash --resume journal.json` continues without repeating finished steps.
  **Stop** waits for the current step; a running write is never interrupted.
- If the phone drops off USB or stops answering (fastboot says `Status read failed`,
  `Write to device failed`, `Too many links` or `no link`, or a question gets no reply), the flash
  stops right there. A silent phone is never taken for "this partition doesn't exist". That's the
  USB link, not the ROM: restart the phone into the bootloader (force it if it's stuck), use a port
  on the PC itself (a rear port, USB 2.0 if there is one, no hub or front-panel port) and a good data
  cable, then press **Resume**. The log names the fastboot used; if it's an old copy, put a current
  `platform-tools` folder next to OPxFlasher (it's used first).
- **Android 17 on a OnePlus 13:** the flash dialog asks for the phone's hardware. China hardware
  (PJZ110, also converted to Global) keeps its Android 16 `abl` and `engineering_cdt` and
  never uses 17's fastboot (`--hardware cn` in the CLI). Recovery zips of such ROMs ask the same in
  Convert (`-cnhw.zip`); see *Convert to Global*.
- On Windows the switch to fastbootd is sometimes missed until the cable is plugged in again. If the
  phone's screen shows fastbootd and OPxFlasher keeps waiting, it says so after about 20 seconds:
  unplug the cable and plug it back in. **Resume** also waits for the phone, and switches it between
  the bootloader and fastbootd if it came back in the other mode.
- **Keep my custom recovery** only works when nothing is written from fastbootd. fastbootd starts
  from the recovery partition, and a custom recovery made for another build may not start with the
  new boot images (AERA for 16 under a 17 ROM never brought fastbootd up). For a full ROM on the
  OnePlus 13 the ROM's own recovery is therefore always written; put your custom recovery back
  afterwards with **Flash image**.
- The bootloader images (boot, dtbo, init_boot, modem, recovery, vbmeta*, vendor_boot on the
  OnePlus 13) go to `_a` then `_b` with no questions in between, the way Super Flasher writes them.
- Optional: factory reset (`--wipe`), don't reboot at the end (`--no-reboot`: the phone stays in the bootloader,
  or goes back to it from fastbootd),
  flash the ROM's region files (`--include oplusstanvbk,my_region`, usually off).

## Restore the whole ROM from a backup

A **Full firmware** backup is an offline copy of your phone's whole Android OS (never your
personal data). If the ROM breaks, put it back:

- **App:** Backup tab → **Use a backup → Restore full ROM...** → choose the backup folder.
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

- **App:** Backup tab → **Use a backup → Make recovery zip...** → pick the recovery → choose the backup.
- **Command line:** `opx restore-zip <backup-folder> --out <folder> [--recovery aera|orangefox]`
  (`--compress` for a smaller file).
- Before anything is packed, every image is re-hashed against the SHA-256 recorded when the backup
  was taken; a backup that was damaged on disk since then is refused and no zip is made.

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
  (both slots), and `my_region` when the new ROM's vbmeta doesn't check it, back
  **from your backup**, so the modem keeps the setup that works with your SIM.

Some phones use a different device codename in each region (OnePlus 13: `OP5D0DL1` China,
`OP5D55L1` EU / India). Those listed in the phone's profile count as the same phone, so a Global ROM on
a Chinese OnePlus 13 reaches this conversion mode. After converting, the phone reports its new model
(e.g. CPH2653); your backup from before still counts as this phone's, because the serial number
matches. Step by step: **[Convert to Global](CONVERT_TO_GLOBAL.md)**.

**Signal restore on its own** (no ROM), for after an update took the signal away:
the *Signal / region* tab or `opx region restore`. Restore `oplusstanvbk` first; restore
`my_region` only from a backup of the build that's installed now, because the installed vbmeta may
check it at boot.

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
   **First root:** a phone without root can't be backed up yet, so tick *Flash without a backup*
   (*I agree with the risk*; CLI: type `ROOT NO BACKUP`). Only init_boot / boot of the current slot is written, and the stock
   image from step 1 stays ready for *Restore stock image*. Make your Critical and Full backups
   right after.
5. **Unroot** writes the stock image back the same way (typed `UNROOT`).

After an OTA, run the same steps with the new build's full OTA.

## Unlock the bootloader

**Phone → Unlock bootloader...** (or `opx unlock`) unlocks with the phone maker's own command
(`fastboot flashing unlock`, or `fastboot oem unlock` on older OnePlus bootloaders); the phone
then asks you to confirm on its own screen. **Unlocking erases everything on the phone.**

1. Before you start: back up your photos and files, and turn on **OEM unlocking** and **USB
   debugging** in Developer options (Settings → About phone: tap the build number 7 times).
2. Read the warnings (security, warranty, data wipe, no liability): the button stays off for 30
   seconds, then answer a small sum and tick *I understand the risks of my action*. A last
   **Are you sure?** defaults to No.
3. OPxFlasher checks the phone: already unlocked → nothing to do; OEM unlocking off → it stops and
   says how to turn it on.
4. It reboots to the bootloader (never fastbootd, and not on firmware where that reboot is known to
   brick), checks `unlocked` again, and sends the unlock command.
5. On the phone, choose **UNLOCK THE BOOTLOADER** with the volume keys and press power. The phone
   erases itself and restarts; set it up again and turn USB debugging back on.

OPPO and realme phones usually also need the maker's Deep Testing approval before the bootloader
accepts the command.

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
bigger than the partition, firmware with a broken bootloader, and the **bootloader chain**
(`abl`, `xbl`, `xbl_config`, `tz`, `hyp`, ...): one from another build can trip Qualcomm
anti-rollback, and a phone that can't start its bootloader can't be fixed with fastboot, so
those are only written as part of a full ROM after the ROM check. `--disable-verity`
(vbmeta only) flashes with verity and verification off. A Critical or Full backup of this
phone is required, the image is re-hashed right before writing, and you confirm by typing
FLASH. If the phone is already in fastboot, the backup tells OPxFlasher which phone it is.

**Without a backup** (e.g. installing a custom recovery on a phone that isn't rooted yet):
tick *Flash without a backup: I agree with the risk* (CLI: `--no-backup`, type `NO BACKUP`).
Everything above is still refused (identity partitions, `super`, the bootloader chain, wrong
types, firmware with a broken bootloader); only the backup isn't required. The phone can
already be in fastboot: OPxFlasher knows it by its serial number. A full ROM flash works the same
way (see ROM check).

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

**If the catalog goes away.** **ROMs → ROM catalog → Save ROM catalog snapshot...** keeps the whole list (every
phone, region and build with its official link, size and MD5) in one JSON file; save one now and
then. When the catalog can't be reached, OPxFlasher tries the mirror at `ota.ox1de.xyz`, then any
mirrors you added (**ROMs → ROM catalog → ROM catalog mirrors...**: add, test or remove a server that speaks the
same API, e.g. your own copy of opx-ota-mirror), then the last list it downloaded, then your snapshot (**ROMs → ROM catalog → Use a ROM catalog snapshot...** picks a
different one). Downloads always come straight from OPPO / OnePlus servers; the snapshot only
holds the links. Command line: `opx ota snapshot --out <folder>`, `opx ota mirrors --add <host>`.

- **App:** ROMs tab → **Official ROMs...** (or the ROMs menu) (connect the phone first). ROMs for your
  own model are listed first; other variants (regions) are marked and ask before
  downloading, because flashing them changes the region and needs a data wipe.
  - **Download only** saves the full OTA zip (MD5 checked) and runs the ROM check on it. The file is
    OPPO's package name followed by the OS version, so you can see which build it is, e.g.
    `PJZ110_11.F.07_2070_202610080010-17-0-1-202.zip` for PJZ110_17.0.1.202(CN01).
  - **Download and convert** also makes the full ROM you tick under *Convert for*:
    **OPxFlasher (image folder)** is ticked by default; tick **OrangeFox / AERA (recovery zip)**
    to get a recovery zip as well. If converting fails or is stopped, the download is kept.
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
- **Names show the OS version and end with the region** (CN, IN, EU, NA, GLO ...), from the phone
  model, e.g. `PJZ110_11.F.07_2070_202610080010-17-0-1-202-fullrom-CN.zip` or
  `CPH2649_11.H.05_3050_202610210133-17-0-0-102-fullrom-IN`. The version comes from the package's name
  or its metadata; when the version or the region can't be told for sure, it's left off rather than
  guessed.
- **Already converted?** If the output is already in that folder, OPxFlasher asks: **Verify and
  use** it (fully verified first, never reused if damaged) or **Replace** it. *Download and
  convert* verifies and reuses it automatically.
- **What to make:** the **OPxFlasher image folder** is ticked by default; tick the **recovery zip**
  too if you want to install from OrangeFox / TWRP / AERA (`--folder` / `--no-zip` on the command
  line).
- **Recovery zip** (`<name>-fullrom-<REGION>.zip`) for **OrangeFox, TWRP and AERA**. Copy it to the phone and
  flash it. Before writing anything the installer checks:
  - it's the phone the ROM is for (its device codename; the chip platform name, shared by many
    phones, never counts), and the **same variant**: a normal zip never changes region, which needs
    a data wipe (a **convert ready** zip does, see below). The installed model and build are read from `my_manifest`, `odm`, `vendor`,
    `product`, `system_ext` and `system`,
  - the ROM is **not older than the installed Android** (the newest build date of the installed
    partitions; reinstalling the exact installed build is fine): installing older software can blow
    the OnePlus 13 / 13T / 15 anti-rollback fuse,
  - no system update is half-applied,
  - every partition exists and is big enough, and `lptools` is available (for system, vendor, odm...),
  - **every file in the zip** matches its SHA-256.

  **Your network files stay yours:** `oplusstanvbk` (and `my_region` when the ROM's vbmeta doesn't
  check it at boot) isn't in the zip, so the phone keeps its own copy, exactly as when flashing from
  the PC. The image folder still contains them for OPxFlasher to decide.

  **Keep my custom recovery** (`--keep-recovery`): the ROM's recovery is left out of the zip
  (`...-keeprec.zip`), so AERA / OrangeFox stays installed. The Flash dialog has the same option
  when flashing from the PC.

  **Convert ready** (`--ready-for <backup>`): for changing region (e.g. China → Global) with one
  flash in recovery. The zip (`...-ready-<model>.zip`) carries **this phone's own** `oplusstanvbk` /
  `my_region` from your Network essentials or Critical backup (checked against the backup's
  SHA-256; ones the new ROM's vbmeta checks at boot come from the ROM). It **only installs on the
  phone the backup was made on** (serial number). In recovery it asks twice, with the volume keys
  (**Vol+ = yes, Vol- = no; no answer in 30 seconds = no**):
  1. when the model changes (e.g. PJZ110 → CPH2653): flash this ROM, at your own risk? No writes
     nothing;
  2. after the ROM is written: flash your own network files from the backup? No keeps the
     ones the phone has (restore yours later from Signal / region if needed).

  Then it **formats data** with the recovery's `twrp` command. If formatting fails it says so:
  format data yourself before rebooting. A convert ready zip is made on its own, never shared: it
  holds your phone's files. **Any earlier OPxFlasher zip** (`...-fullrom-*.zip`) can be made convert
  ready: open it in Convert and tick Convert ready.

  Then it writes the current slot (firmware, boot images, the logical partitions resized inside
  super, `vbmeta` last) and reads everything back. Images over 1 GiB are stored in parts, because
  some recoveries can't unzip bigger files.
- **Image folder** (`<name>-fullrom-<REGION>/`): plain images plus `opxflasher-rom.json`, for fastboot or
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
profile can lock more (`keep_apps`: exact names, `prefix*` or `*contains*`). On OPPO / OnePlus /
realme phones that includes the ColorOS core services (athena, battery, security, cosa, smart
engine, scene service ...), Play services, WebView, overlays and root managers.

**Built-in lists** (drafts for ColorOS 16 CN, to be confirmed on a OnePlus 13; from the OP Debloater
catalog). Each shows exactly which apps it will remove and asks first:

| List | What it removes |
|---|---|
| Minimal | Ads, telemetry and partner stubs. Safe for everyone. |
| Recommended | Minimal plus stores, CN media, browser feeds, Breeno voice. |
| Aggressive | Recommended plus caution-level extras (marked `[caution]` in the file). |
| Third-party apps | Douyin, Weibo, JD, Baidu and other preinstalled China apps. |
| Breeno / AI | The Breeno assistant and AI suite. |
| HeyTap / OPPO China services | HeyTap accounts, cloud, wallet and other China services. |
| Google extras | Optional Google apps (Meet, YouTube Music, TV, One, Gemini ...). Play services stay. |

- Remove or restore selected apps; **Restore all I removed** undoes everything OPxFlasher removed on this phone.
- **Built-in lists** menu, or share lists: **Save my list...** writes what you removed (one package per line); **Remove from list...** applies someone else's list, skipping anything locked or not on your phone.
- Command line: `opx apps list [--removed] [--search X]`, `opx apps remove <pkg>... | --from list.txt` (the built-in lists are in OPxFlasher's `debloat` folder),
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
  page) applied in Device Manager, step by step (**Copy steps** puts them on the clipboard). In
  **fastbootd**, OnePlus / OPPO phones show up in Windows as **`ossi`**: pick *Android Bootloader
  Interface* for it in Device Manager (Update driver > Browse my computer > Let me pick > Android
  Device); if bootloader mode already worked, that driver is already installed. On Linux it checks your user may open the phone (udev rules /
  `plugdev`). It also spots a phone stuck in Qualcomm 9008 (EDL) mode. OPxFlasher never installs
  drivers, opens Windows settings or asks for administrator rights itself; only the driver install
  asks for approval.

## Anti-rollback you confirm

When OPxFlasher can't read the Qualcomm anti-rollback (ARB) version (no root, or a reader not yet
validated for the phone), the Flash dialog asks you to **Read the warning and accept the risk**: a
serious warning (a lower ARB bricks the phone; sometimes only an official OPPO / OnePlus service
centre can repair it), a minute of reading, a small sum, your agreement and a last "Are you sure?".

**Validate ROM / ARB** (ROM check row, and in that warning) helps you decide: with the phone running
Android it compares the ROM with the installed build. Same or newer: anti-rollback stays the same or
goes up. **Older: don't flash it** — the warning then can't be accepted. The result is saved for
that phone and ROM, so it's shown later even with the phone in fastboot. In the CLI you type
`ARB CHECKED`.

## ROMs and Flash ROM

- **ROMs** tab: get (Official ROMs), verify and convert ROMs. Nothing touches the phone.
- **Flash ROM** tab: choose the ROM (zip, image folder, or a folder of `.img` files you extracted
  yourself), **Check ROM**, **Flash**. The phone can be in Android, recovery, fastboot or fastbootd.
  - In Android, OPxFlasher reads which build is installed. In fastboot only (e.g. Android doesn't
    start) it knows the phone from the bootloader (OnePlus 13: `dodge`), and in the anti-rollback
    warning you type the installed build (Settings > About) to compare, or confirm it yourself.
    Not sure? Cancel and flash the newest official ROM for your phone: it can't be older.
  - A folder of loose `.img` files has no metadata saying which phone or build it's for: you
    confirm both in the same warning. Identity partitions, sizes and the boot chain are still checked.
  - **Network files backup** (in the Flash dialog): pick your Network essentials backup to write
    your own `oplusstanvbk` / `my_region`, separately from the main backup.
