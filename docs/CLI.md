# Command-line reference

The `opx` command does everything the app does. Install it with pip or pipx (see the
[README](../README.md#download)); the portable `.exe` and AppImage contain only the app.
Every command has `--help`, and commands that act on a phone take `-s <serial>` when more
than one is connected.

Commands that write to the phone always show their checks first and ask you to type a
confirmation word (`FLASH`, `RESTORE`, `ROOT` ...). Most also have `--dry-run`.

## Phone

```sh
opx devices                          # connected phones (adb and fastboot)
opx info                             # model, variant, build, slot, root access, warnings
opx reboot system|bootloader|fastbootd|recovery
opx usb-check                        # phone not showing up? drivers / permissions
opx rescue                           # phone won't start? state + what to try
opx edl-info [--port COM5]           # identify a phone in 9008 (EDL) mode, read-only
opx tools fetch | where              # download / locate adb and fastboot
opx about                            # version, build, developer and links
```

## Backup

```sh
opx partitions --mode critical               # what a backup would read
opx partitions --all [--csv parts.csv]       # every partition with size, slot and role
opx backup --mode network|critical|full --out backups [--dry-run]
opx verify backups/<folder>                  # re-check a backup offline
opx unlock                                   # unlock the bootloader (ERASES the phone; asks 3 times)
```

## ROMs

```sh
opx ota list [--model PJZ110]                # official ROMs (Daniel Springer's catalog)
opx ota download --id <id> --out ~/Downloads [--connections 4]   # resumes if run again
opx ota snapshot --out ~/Backups               # whole ROM catalog in one file (for when it's down)
opx ota mirrors --add ota.example.com          # add your own catalog mirror (tried after ota.ox1de.xyz)
opx rom detect <file-or-folder>                # what is it? phone, build, contents, what's missing
opx rom verify <rom> [--quick] [--json]        # complete and undamaged? who can install it?
opx rom convert <rom> --out ~/ROMs [--folder] [--no-zip]   # full ROM: recovery zip and/or image folder

opx rom inspect ROM.zip                      # device, build, full/incremental, images
opx rom extract ROM.zip init_boot vbmeta     # pull images out (hash-verified)
opx rom avb vbmeta.img                       # AVB rollback index and descriptors
opx rom arb xbl_config.img                   # Qualcomm anti-rollback (experimental)

opx plan ROM.zip [--backup backups/<folder>] [--json]   # check a ROM, flash nothing
```

## Flash and restore

```sh
opx flash ROM.zip --backup backups/<folder> [--allow-fastbootd] [--wipe] [--dry-run]
opx flash --resume opx-flash-<time>/journal.json        # continue an interrupted flash

opx flash-image <partition> <file.img> --backup backups/<folder> [--slot current|other|both]
                [--disable-verity] [--dry-run]

opx restore-rom backups/<full-backup> [--wipe] [--not-updated] [--dry-run]
opx restore-zip backups/<full-backup> --out <folder> [--recovery aera|orangefox] [--compress]

opx region show --backup backups/<folder>
opx region restore --backup backups/<folder> [--allow-fastbootd]
```

On a region change, `opx flash` restores your own region files automatically
(`--no-restore-region` turns that off) and refuses to run without `--wipe`.

## Root

```sh
opx root prepare --manager magisk|kernelsu|apatch --rom FULL_OTA_OF_CURRENT_BUILD.zip
opx root pull   --work opx-root-<model>-<time>          # after patching on the phone
opx root flash  --work opx-root-<model>-<time> --backup backups/<folder>
opx root unroot --work opx-root-<model>-<time> --backup backups/<folder>
```

## Apps and diagnostics

```sh
opx apps list [--removed] [--search heytap]
opx apps remove <package>... | --from debloat.txt [-y]
opx apps restore <package>... | --all
opx apps export debloat.txt                  # what OPxFlasher removed on this phone

opx logs [--out <folder>]                    # logcat, kernel log, last crash; serials hidden
opx logs --read <logs.zip>                   # known-problem hints from saved logs
```

## Device profiles

```sh
opx profile list
opx profile check my-phone.json
opx profile install my-phone.json
```

See [Adding a device](ADDING_A_DEVICE.md).

## Extras

Some newer OTAs compress with zstd: install the `zstd` extra (`pip install "opxflasher[zstd]"`,
included in the portable apps).
