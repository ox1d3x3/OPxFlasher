# Contributing to OPxFlasher

Thanks for helping. Bug reports, device profiles, translations, debloat lists and code are all
welcome.

## Reporting a problem

Open an [issue](https://github.com/ox1d3x3/OPxFlasher/issues/new/choose) and include:

- the OPxFlasher build (shown in the window title, e.g. `0.7.0-rc.12`) and your OS;
- the phone model and build (e.g. `PJZ110_16.0.3.501(CN01)`);
- what you did, what you expected and what happened;
- the zip from **Export log...** (serial numbers are hidden). For a phone that won't start, also
  the zip from **Diagnostics → Save phone logs...** if you can get one.

Please don't post full serial numbers, IMEIs or backup files publicly.

## Development setup

```sh
git clone https://github.com/ox1d3x3/OPxFlasher
cd OPxFlasher
python -m pip install -e ".[dev,zstd]"
python -m pytest                                   # the whole suite, no phone needed
ruff check src tests packaging scripts             # lint (settings in pyproject.toml)
python scripts/extract_strings.py --check          # translation template is current
opxflasher                                         # run the app
```

The tests never touch a real phone. `tests/fake_adb.py` serves partitions from a folder,
`tests/fake_fastboot.py` simulates the bootloader and fastbootd (including firmware where a
reboot bricks the phone), and `tests/romfactory.py` builds synthetic OTAs, vbmeta images and
signed ELFs. New features should come with tests against these simulators.

## Pull requests

- Keep the safety model: anything that can't be confirmed safe must block (fail closed), and
  identity partitions and personal data stay on the never-flash list.
- Commands sent to the phone are built in one place (`src/opx/remote.py` for read-only access)
  and validated before use; don't build shell strings from untrusted input.
- New user-facing text goes through `tr()`; run `python scripts/extract_strings.py` afterwards.
- CI must pass on Windows, Linux and macOS, plus the lint job and both portable builds.

## Device profiles and translations

See [Adding a device](docs/ADDING_A_DEVICE.md) and [Translating](docs/TRANSLATING.md).
