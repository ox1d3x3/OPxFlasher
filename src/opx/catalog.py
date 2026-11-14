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
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opx import __version__, otadl
from opx.tools import user_tools_dir

API = "https://roms.danielspringer.at/api/ota.php"
# Mahabub's self-hosted mirror (same API), asked only when the main catalog can't be reached
MIRRORS = ("https://ota.ox1de.xyz/api/ota.php",)
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
          cache: Path | None = None, opener=None, mirrors: tuple[str, ...] | None = None,
          snapshot: Path | None = None) -> list[Release]:
    """Releases matching the filters, newest version last (catalog order).

    Tries the catalog, then each mirror, then the last copy on disk, then a saved
    snapshot (settings 'catalog_snapshot'). Mirrors serve the same API."""
    global last_source
    errors = []
    for base in sources() if mirrors is None else (API, *mirrors):
        try:
            rels = _fetch_from(base, model, device, region, latest, cache, opener, offline_ok=False)
            last_source = base
            return rels
        except CatalogError as e:
            errors.append(e)
    last_source = "saved copy"
    try:  # every server down: the last copy we have
        return _fetch_from(API, model, device, region, latest, cache, opener, offline_ok=True, network=False)
    except CatalogError:
        pass
    snap = snapshot if snapshot is not None else _snapshot_setting()
    if snap is not None and snap.exists():
        return filter_releases(load_snapshot(snap), model, device, region, latest)
    raise errors[0]


# --- mirrors: the built-in ones plus any the user adds (same API) ----------------------------

last_source = API  # which server answered the last fetch (for "from mirror ..." notes)


def normalize_mirror(url: str) -> str:
    """'ota.example.com' / 'https://ota.example.com/' -> 'https://ota.example.com/api/ota.php'."""
    url = (url or "").strip()
    if not url:
        raise CatalogError("Enter a mirror address.")
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise CatalogError("A mirror address starts with https:// (or http:// on your own network).")
    path = parts.path.rstrip("/")
    if not path.endswith((".php", "/api/ota")):
        path += "/api/ota.php"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def user_mirrors() -> list[str]:
    from opx import settings

    value = settings.get("catalog_mirrors") or []
    return [str(u) for u in value if isinstance(u, str) and u.strip()] if isinstance(value, list) else []


def set_user_mirrors(urls: list[str]) -> None:
    from opx import settings

    settings.put("catalog_mirrors", list(dict.fromkeys(urls)))


def sources() -> list[str]:
    """Where the catalog is looked up, in order: the catalog, built-in mirrors, user mirrors."""
    return list(dict.fromkeys((API, *MIRRORS, *user_mirrors())))


def describe_source(base: str) -> str:
    if base == API:
        return "the OTA catalog"
    return urllib.parse.urlsplit(base).hostname or base if base.startswith("http") else base


def test_mirror(url: str, opener=None) -> int:
    """Ask a mirror for its latest builds; returns how many it has. Raises CatalogError."""
    base = normalize_mirror(url)
    with tempfile.TemporaryDirectory() as tmp:
        rels = _fetch_from(base, "", "", "", True, Path(tmp), opener, offline_ok=False)
    if not rels:
        raise CatalogError("That server answered, but has no ROMs listed.")
    return len(rels)


def _snapshot_setting() -> Path | None:
    from opx import settings

    value = settings.get("catalog_snapshot")
    return Path(value) if value else None


def filter_releases(releases: list[Release], model="", device="", region="", latest=False) -> list[Release]:
    out = [r for r in releases
           if (not model or r.model.upper() == model.upper())
           and (not device or r.device.lower() == device.lower())
           and (not region or r.region.upper() == region.upper())
           and (not latest or r.is_latest)]
    return out


def _fetch_from(base: str, model, device, region, latest, cache, opener, offline_ok: bool,
                network: bool = True) -> list[Release]:
    query = {k: v for k, v in (("model", model), ("device", device), ("region", region)) if v}
    if latest:
        query["latest"] = "1"
    url = base + ("?" + urllib.parse.urlencode(query) if query else "")
    opener = opener or urllib.request.urlopen

    cache = cache or cache_dir()
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    body_file, etag_file = cache / f"{key}.json", cache / f"{key}.etag"
    headers = {"Accept": "application/json"}
    if body_file.exists() and etag_file.exists():
        headers["If-None-Match"] = etag_file.read_text(encoding="utf-8").strip()

    if not network:
        if not body_file.exists():
            raise CatalogError("No saved copy of the catalog.")
        body, etag = body_file.read_bytes(), ""
    else:
        try:
            with opener(_request(url, headers), timeout=20) as resp:
                body = resp.read()
                etag = resp.headers.get("ETag", "")
        except urllib.error.HTTPError as e:
            if e.code == 304 and body_file.exists():
                body, etag = body_file.read_bytes(), ""
            else:
                raise CatalogError(f"The OTA catalog answered HTTP {e.code}.") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if offline_ok and body_file.exists():
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


# --- snapshots: the whole catalog in one file (back-pocket copy / mirror seed) ---------------

SNAPSHOT_FORMAT = "opxflasher-catalog-snapshot"


def snapshot(out: Path, opener=None, progress=None) -> dict:
    """Save every release the catalog lists to `out` (same JSON shape as the API, plus
    a 'snapshot' header), so it can be used offline or served by a mirror as-is.
    Asks for the whole list, then each device on its own (in case the full list is
    capped), and merges by release id. Returns {"count", "devices", "path"}."""
    seen: dict[str, dict] = {}

    def grab(query: dict) -> list[dict]:
        url = API + ("?" + urllib.parse.urlencode(query) if query else "")
        try:
            with (opener or urllib.request.urlopen)(_request(url, {"Accept": "application/json"}), timeout=60) as r:
                data = json.loads(r.read())
            return list(data["releases"])
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as e:
            raise CatalogError(f"Can't read the OTA catalog ({url}): {e}") from e

    for r in grab({}):
        seen.setdefault(str(r.get("id")), r)
    devices = sorted({str(r.get("device")) for r in seen.values() if r.get("device")})
    for i, dev in enumerate(devices, 1):
        if progress:
            progress(dev, i, len(devices))
        for r in grab({"device": dev}):
            seen.setdefault(str(r.get("id")), r)
    releases = sorted(seen.values(), key=lambda r: (str(r.get("device")), str(r.get("model")),
                                                     str(r.get("build_timestamp")), str(r.get("version"))))
    for r in releases:  # every entry must still parse
        Release.from_json(r)
    from datetime import datetime, timezone

    doc = {"api_version": "1", "count": len(releases), "releases": releases,
           "snapshot": {"format": SNAPSHOT_FORMAT, "source": API, "credit": CREDIT,
                        "saved_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "tool": USER_AGENT, "devices": devices}}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return {"count": len(releases), "devices": len(devices), "path": out}


def load_snapshot(path: Path) -> list[Release]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [Release.from_json(r) for r in data["releases"]]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise CatalogError(f"{Path(path).name} isn't a readable catalog snapshot.") from e


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
