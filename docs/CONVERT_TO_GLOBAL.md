# Converting a Chinese OnePlus 13 to Global / EU / India

A step-by-step for turning a **PJZ110** (China, ColorOS) into a **CPH2653** (EU / Global) or
**CPH2649** (India) OnePlus 13 with OPxFlasher. Read it all before you start.

> **Not tested on a real phone yet.** OPxFlasher's conversion has only run against a simulated
> phone. Until it has been confirmed on a real OnePlus 13, treat every step as a test: stop at the
> first thing that looks wrong and keep your logs (**Export log...**).

## What you need to know first

- **It wipes the phone.** A region change always needs a factory reset; OPxFlasher won't convert
  without one. Your photos, apps and accounts are gone afterwards.
- **The bootloader must be unlocked**, and the phone **rooted** for the backup OPxFlasher requires.
  On ColorOS 16+, Chinese phones need OnePlus's approval to unlock, which can take a week or more.
- **Don't relock the bootloader afterwards.** If anything on the phone doesn't verify, a locked phone
  won't start, and only an authorised repair centre (EDL) can recover it.
- **Never type `adb reboot fastboot`** on ColorOS 17 CN. It has bricked phones. Use OPxFlasher's
  Reboot buttons; on ColorOS 17 CN the app refuses to flash at all (go back to ColorOS 16 with the
  official rollback first).
- **There's no going back to an older build.** From ColorOS 16.0.3.501 the phone has an
  anti-rollback fuse: an older build than the one installed can brick it for good. OPxFlasher compares
  version numbers and refuses older builds, so:
  - the Global build you pick must have the same or a higher version number than the CN build
    installed now;
  - after converting, your CN backup can't take you back if it's older than the Global build. The
    way back is an official **CN** ROM of the same or a newer version.
- **North America (CPH2655)** isn't supported for conversion yet: its device codename hasn't been
  confirmed, so the ROM check stops.

### Global 16 or Global 17?

A ColorOS / OxygenOS 17 bootloader (`abl`) is reported to leave fastboot mode within a second on
Chinese OnePlus 13 hardware. If that's true, after flashing a 17 build you can't use the
bootloader's fastboot mode any more (fastbootd and recovery still work). OPxFlasher's conversion is
built not to need it after the first write, but **Global 16 is the more cautious first step**: it
keeps a 16 bootloader, and the phone can move to 17 later.

## 1. Before converting

1. **Look first.** Settings → About: model, ColorOS version, build number. Write them down.
2. **Developer options** → USB debugging and OEM unlocking on. Connect; the phone panel fills in.
3. **Unlock** the bootloader if it isn't (Phone → Unlock bootloader...), then **root** (Root tab).
4. **Backups:** Backup → **Critical**, then **Full firmware**. Both must end with every image ok.
   **Use a backup → Verify a backup...** on both, then copy them to a second drive.

## 2. Get and check the Global ROM

1. **Official ROMs...** → tick *Also show other variants* → pick the CPH2653 (or CPH2649) build →
   **Download and convert** (OPxFlasher image folder). The file name ends with the version, e.g.
   `...-16-0-10-600.zip`.
2. **Verify ROM...** on the download: it must say VERIFIED. Its *Identity* line shows the ROM's
   **hardware project**.
3. **Check ROM** with the phone connected, on the converted folder. Expect:
   - *Device match*: **OK**, "same phone, other region";
   - *Variant*: **STOP**, a region change (you confirm it when flashing);
   - *Qualcomm ARB*: **STOP**, reader not validated (you confirm it after checking the numbers);
   - *Rollback fuse*: OK (if not, the build is older than yours: pick a newer one);
   - *Project*: OK, or a warning that the phone's own updater will refuse this ROM's updates
     (then later updates have to go through OPxFlasher too).

   Anything else that stops: don't continue. **Save report...** and ask.

## 3. Flash

In the Flash dialog:

- **Wipe data (factory reset)**: on (required for a region change).
- **Use fastbootd for logical partitions**: on (the ROM's system partitions are flashed there).
- **Region conversion → Keep mobile signal**: on (recommended). Your phone's own `oplusstanvbk`, and
  `my_region` when the new ROM doesn't check it at boot, are written back from your backup.
- **Different variant → Yes, I want to flash this other variant**.
- Type `ARB CHECKED` (only after the numbers were checked) and `FLASH`.

The flash goes: reboot to the bootloader → check it's unlocked → **wipe** → firmware to both slots
→ fastbootd → system partitions and your region files → **reboot**. It never goes back to the
bootloader after the first write. The first boot takes a few minutes.

If a step fails, don't reboot or unplug: **Export log** and resume from the journal when the cause
is fixed.

### Or from a custom recovery (AERA / OrangeFox)

Convert with **Convert ready** ticked and your backup chosen (`opx rom convert <rom> --ready-for
<backup>`; a `...-fullrom-*.zip` you made earlier works too): you get `...-ready-PJZ110.zip`. Copy it
to the phone and flash it in recovery. It checks it's your phone (serial number) and that the ROM
isn't older, asks **Vol+ to flash the new model** (Vol- or 30 seconds = cancel, nothing written),
writes the ROM, asks **Vol+ to write your own network files** (Vol- or 30 seconds = skip), then
formats data. Tick **Keep my custom recovery** too if AERA / OrangeFox should stay.
Don't share this zip: it holds your phone's files.

## 4. After the first boot

- Settings → About: model **CPH2653** (or CPH2649), OxygenOS.
- **SIM, calls, mobile data, VoLTE**, and `*#06#` shows your IMEI.
- Wi-Fi, Bluetooth, fingerprint, camera.
- **Software update**: does it check without errors? Does it offer anything?
- No signal? **Signal / region** tab → restore `oplusstanvbk` from the backup you made before
  converting (it still counts as this phone's: the serial number matches).
- Make **new Critical and Full backups** of the converted phone; they're your restore point from now
  on.
