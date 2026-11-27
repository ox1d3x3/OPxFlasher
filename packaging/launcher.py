"""Entry point frozen into the portable builds (OPxFlasher.exe / .AppImage)."""

import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["mcp"]:
        # Claude Desktop starts the app as its MCP server: no window, MCP over stdio
        from opx.cli import main as cli_main

        sys.exit(cli_main(sys.argv[1:]))
    from opx.gui.app import main

    sys.exit(main())
