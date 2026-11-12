# Adding a phone to OPxFlasher

OPxFlasher knows each phone through a small JSON **profile**. Adding your phone means
writing one profile, trying it locally, and opening a pull request.

## 1. Collect the facts (USB debugging on)

```sh
adb shell getprop ro.product.model     # e.g. CPH2581  (one per variant)
adb shell getprop ro.product.device    # codename, e.g. OP595DL1
adb shell getprop ro.build.display.id
adb shell ls /dev/block/by-name        # partition names
```

Ask owners of the other variants (CN / IN / EU / NA / Global) for their `ro.product.model`.

## 2. Write the profile

`oneplus-12.json`:

```json
{
  "id": "oneplus-12",
  "name": "OnePlus 12",
  "inherits": "oplus-generic",
  "match": { "ro.product.model": ["PJD110", "CPH2581", "CPH2583", "CPH2573"] },
  "variants": { "PJD110": "China", "CPH2581": "EU", "CPH2583": "NA", "CPH2573": "India" },
  "recovery_boards": ["waffle"],
  "notes": "Where the variant list came from, anything special about this phone."
}
```

- `inherits` must be `oplus-generic`; you get its backup lists and its **never-flash** list.
  You can add partitions to `never_flash`, never remove them.
- `known_issues` (optional): builds that brick on certain reboots, e.g.
  `{"match": {"ro.build.display.id": "CPH2581_15.0.0.1*"}, "severity": "danger", "blocks": ["fastbootd"], "message": "..."}`.
  `blocks` may contain `fastbootd` and `bootloader`.
- `recovery_boards` (optional): the board name custom recoveries report (for restore zips).
- Leave `qcom_arb_validated` out. It's only set after anti-rollback numbers are checked
  against real firmware.

## 3. Try it

```sh
opx profile check oneplus-12.json      # validates it
opx profile install oneplus-12.json    # copies it to your user profiles folder
opx info                               # should now say "Profile: OnePlus 12"
```

User profiles never replace built-in ones; remove the file from the folder
`opx profile list` shows to uninstall it.

## 4. Send it

Put the file in `src/opx/profiles/` and open a pull request. CI checks every bundled
profile (`tests/test_profiles.py`), on Windows, Linux and macOS. Please say which phone you
tested it on and what you ran (backup? ROM check?).
