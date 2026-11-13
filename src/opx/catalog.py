"""Official OTA packages from Daniel Springer's public OTA catalog.

Catalog: https://roms.danielspringer.at (API: /api/ota.php, read-only, v1).
Thanks to Daniel Springer for running it and allowing OPxFlasher to use it.

Be a good guest: responses are cached on disk and revalidated with ETag
(an unchanged catalog answers 304), and nothing polls in the background; the
catalog is only asked when the user opens it.

The catalog lists each release's *source* link (an official OPPO/OnePlus
server). OPxFlasher downloads it the way the phone's updater does (see
opx.otadl), resumably, and checks size, MD5 and that it is a zip.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opx import __version__, otadl
from opx.tools import user_tools_dir

API = "https://roms.danielspringer.at/api/ota.php"
SITE = "https://roms.danielspringer.at"
CREDIT = "OTA catalog by Daniel Springer (roms.danielspringer.at)"
USER_AGENT = f"OPxFlasher/{__version__} (+https://github.com/ox1d3x3/OPxFlasher)"


class CatalogError(RuntimeError):
    pass


class DownloadPaused(CatalogError):
    """Cancelled; the partial download is kept and resumes next time."""


@dataclass
class Release:
    id: str
    device: str
    region: str
    model: str
    version: str
    ota_version: str = ""
    build_timestamp: str = ""
    security_patch: str = ""
    md5: str = ""
    size: int = 0
    published: int = 0
    source_url: str = ""
    changelog_url: str = ""
    is_latest: bool = False
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict) -> Release:
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kw = {k: d[k] for k in known if k in d and d[k] is not None}
        kw["size"] = int(kw.get("size") or 0)
        kw["published"] = int(kw.get("published") or 0)
        kw["is_latest"] = bool(kw.get("is_latest"))
        return cls(**kw, extra={k: v for k, v in d.items() if k not in known})

    @property
    def size_text(self) -> str:
        if not self.size:
            return "unknown size"
        return f"{self.size / 1e9:.2f} GB" if self.size >= 1e9 else f"{self.size / 1e6:.0f} MB"

    @property
    def filename(self) -> str:
        base = self.ota_version or self.version or self.id
        safe = "".join(c if c.isalnum() or c in "._-()" else "_" for c in base)
        return f"{safe}.zip"


def cache_dir() -> Path:
    return user_tools_dir().parent / "catalog"


def _request(url: str, headers: dict | None = None):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})


def fetch(model: str = "", device: str = "", region: str = "", latest: bool = False,
          cache: Path | None = None, opener=None) -> list[Release]:
    """Releases matching the filters, newest version last (catalog order)."""
    query = {k: v for k, v in (("model", model), ("device", device), ("region", region)) if v}
    if latest:
        query["latest"] = "1"
    url = API + ("?" + urllib.parse.urlencode(query) if query else "")
    opener = opener or urllib.request.urlopen

    cache = cache or cache_dir()
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    body_file, etag_file = cache / f"{key}.json", cache / f"{key}.etag"
    headers = {"Accept": "application/json"}
    if body_file.exists() and etag_file.exists():
        headers["If-None-Match"] = etag_file.read_text(encoding="utf-8").strip()

    try:
        with opener(_request(url, headers), timeout=30) as resp:
            body = resp.read()
            etag = resp.headers.get("ETag", "")
    except urllib.error.HTTPError as e:
        if e.code == 304 and body_file.exists():
            body, etag = body_file.read_bytes(), ""
        else:
            raise CatalogError(f"The OTA catalog answered HTTP {e.code}.") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if body_file.exists():  # offline: use the last copy we have
            body, etag = body_file.read_bytes(), ""
        else:
            raise CatalogError(f"Can't reach the OTA catalog: {e}") from e

    try:
        data = json.loads(body)
        releases = [Release.from_json(r) for r in data["releases"]]
    except (ValueError, KeyError, TypeError) as e:
        raise CatalogError("The OTA catalog sent something OPxFlasher doesn't understand.") from e

    cache.mkdir(parents=True, exist_ok=True)
    body_file.write_bytes(body)
    if etag:
        etag_file.write_text(etag, encoding="utf-8")
    return releases


def for_phone(model: str, **kw) -> list[Release]:
    """The phone's own model first; empty if the catalog doesn't know it."""
    if not model:
        raise CatalogError("The phone's model number is unknown, so OPxFlasher can't pick its ROMs.")
    return fetch(model=model, **kw)


def download(release: Release, dest_dir: Path,
             progress: Callable[[int, int], None] | None = None,
             cancel: Callable[[], bool] | None = None,
             opener=None, stage: Callable[[str], None] | None = None,
             android: str = "", build: str = "", connections: int = otadl.CONNECTIONS) -> Path:
    """Download a release's full package the way the phone's updater does, resumably,
    and check it (size, MD5, zip). Returns the zip path. Raises DownloadPaused when
    cancelled (the partial file stays for resuming), CatalogError otherwise."""
    if not release.source_url.startswith("https://"):
        raise CatalogError("This release has no secure download link.")
    try:
        return otadl.fetch(
            release.source_url, dest_dir / release.filename,
            otadl.updater_headers(release.model, android or "16", build),
            size=release.size, md5=release.md5, connections=connections,
            progress=progress, stage=stage, cancel=cancel, opener=opener)
    except otadl.Paused as e:
        raise DownloadPaused(str(e)) from e
    except otadl.DownloadError as e:
        raise CatalogError(str(e)) from e
