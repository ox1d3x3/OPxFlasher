# Translating OPxFlasher

App text is written in English in the code and wrapped in `tr("...")`. A translation is one
JSON file per language.

## Make a translation

1. Copy `src/opx/locales/template.json` to `src/opx/locales/<code>.json`, where `<code>` is
   the language (`de`, `fr`, `hi`, `bn`) or language plus region (`pt_BR`, `zh_CN`).
2. Set `"language"` to the language's own name (e.g. `"Deutsch"`).
3. Fill in the values. Leave any you don't know empty: those stay English.
4. Keep `{placeholders}` exactly as they are, and keep words like *fastboot*, *adb*,
   *super*, *vbmeta* and partition names untranslated.

```json
{
  "language": "Deutsch",
  "strings": {
    "Backup": "Sicherung",
    "Phone not showing up?": "Telefon wird nicht erkannt?"
  }
}
```

## Try it

Put the file in your own `locales` folder (next to your `profiles` folder, see
[ADDING_A_DEVICE.md](ADDING_A_DEVICE.md)) or in `src/opx/locales/` when running from source,
then start the app with the language set:

```
OPX_LANG=de opxflasher          # Linux / macOS
set OPX_LANG=de && OPxFlasher.exe     # Windows
```

Without `OPX_LANG` the system language is used. `pt_BR` falls back to `pt`, then English.

## For developers

Wrap new user-facing text in `tr()` and refresh the template:

```
python scripts/extract_strings.py          # updates template.json, shows each language's coverage
python scripts/extract_strings.py --check  # what the tests run
```
