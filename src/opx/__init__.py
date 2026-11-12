"""OPxFlasher: AIO tool for OnePlus / OPPO devices."""

__version__ = "0.7.0"

# Portable builds write opx/_build.py with the exact release label (for example
# "0.7.0-rc.3") so the app shows which build it is. Not in git.
try:
    from opx._build import VERSION as __version__  # noqa: F401
except ImportError:
    pass
