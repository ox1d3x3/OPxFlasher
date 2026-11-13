# Credits

OPxFlasher is an independent project written from scratch. No code was copied
from the projects below, but they shaped how the OPlus flashing process is
understood, and they deserve the credit for that groundwork.

## Community work

- **koaaN** – [Flasher_Builder](https://github.com/koaaN/Flasher_Builder), the
  local builder for SuperHybrid / Regional flashers
- **Daniel Springer** – SuperFlasher and the OnePlus OTA catalog
- **nirupha** ([oplus-ota](https://github.com/nirupha/oplus-ota)) and the
  **Oxygen Updater** team – for documenting how OPPO's servers expect the system
  updater to ask for OTA packages
- **docnok63** and **Jonas Salo** – OnePlus 13 flasher packaging
- **FTH PHONE 1902** and **Venkayy** – the original flasher scripts that inspired
  the above
- The **OnePlus / OPPO modding community** on XDA and Telegram, for documenting
  partitions, region files and recovery paths

- **AERA Recovery Project** (https://github.com/AERA-Recovery): OPxFlasher's restore zips
  follow the image-zip layout AERA installs natively. No AERA code is included.

## Third-party software

- **Android SDK Platform-Tools** (adb, fastboot) – Google. Not bundled;
  `opx tools fetch` downloads it from Google under Google's terms.
- **Qt for Python (PySide6)** – The Qt Company, LGPL-3.0.
- **pySerial** – Chris Liechti, BSD-3-Clause (talks to a phone in 9008 mode, read-only).
- **zstandard** (optional) – Gregory Szorc, BSD-3-Clause.
- **Space Grotesk** – Florian Karsten and the Space Grotesk Project Authors,
  SIL Open Font License 1.1. Used for the "OPxFlasher" wordmark; the text is
  converted to outlines, so the font itself is not shipped.

If you contributed to any of the work above and want your credit changed, please
open an issue.
