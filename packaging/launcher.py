"""Entry point frozen into the portable builds (OPxFlasher.exe / .AppImage)."""

import sys

from opx.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
