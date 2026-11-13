"""Download official OTA packages from OPPO / OnePlus servers.

OPPO's servers only hand packages to the phone's own updater: a request
without the updater's User-Agent gets "Access Denied" or an error page, and
``downloadCheck`` links also need the updater's ``userId`` header before they
redirect to a signed CDN link (which expires after about an hour). So this
module asks the way the system updater does, then:

- splits big files over a few connections (HTTP range requests),
- retries dropped connections and renews an expired link by asking again,
- keeps the partial file and its progress, so a cancelled, failed or
  interrupted download resumes where it stopped (also after a restart),
- checks the MD5 (catalog's, else the server's ``x-amz-meta-filemd5``) and
  that the result is a readable zip before it gets its final name.

Plain HTTP/1.1 via urllib; no external downloader (aria2 is reported to have
its connections dropped by the OPPO CDN, and would be one more program for
antivirus to inspect).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

UPDATER_USER_ID = "oplus-ota|16002018"
CHUNK = 1 << 20
SEGMENT_MIN = 64 << 20  # don't split anything smaller than this per connection
CONNECTIONS = 4
RETRIES = 6
SAVE_EVERY = 2.0  # seconds between progress saves (for resume)

Progress = Callable[[int, int], None]
Stage = Callable[[str], None]


def backoff(attempt: int) -> float:
    """Seconds to wait before retry number `attempt`."""
    return min(2 ** attempt, 30)


class DownloadError(RuntimeError):
    pass


class Paused(DownloadError):
    """Cancelled by the user; the partial file is kept for resuming."""


def updater_headers(model: str = "", android: str = "16", build: str = "") -> dict[str, str]:
    """Request headers of the phone's own system updater."""
    device = "; ".join(p for p in (f"Android {android or '16'}", model) if p)
    ua = f"Dalvik/2.1.0 (Linux; U; {device}" + (f" Build/{build})" if build else ")")
    return {"User-Agent": ua, "userId": UPDATER_USER_ID, "Accept-Encoding": "identity"}


@dataclass
class Target:
    url: str  # final URL after redirects (signed CDN link)
    size: int
    md5: str
    ranges: bool


def _status(resp) -> int:
    return getattr(resp, "status", None) or resp.getcode()


def _md5_header(value: str) -> str:
    """x-amz-meta-filemd5 as hex (it may come as hex or base64)."""
    value = (value or "").strip().strip('"')
    if len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.lower()
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return ""
    return raw.hex() if len(raw) == 16 else ""


def _refused(status: int, body: bytes) -> str:
    """Turn a server refusal (error page / JSON) into a sentence."""
    code = ""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        if isinstance(data, dict):
            code = str(data.get("code") or data.get("responseCode") or data.get("errCode") or "")
    except ValueError:
        pass
    what = f"HTTP {status}" + (f", error {code}" if code else "")
    if status in (401, 403) or code:
        return (f"The official server refused the download ({what}). The link may have expired; "
                "refresh the ROM list and try again.")
    if status == 404 or status == 410:
        return f"The official server no longer has this package ({what})."
    return f"The official server didn't send a zip ({what}). Refresh the ROM list and try again."


def probe(url: str, headers: dict[str, str], opener=None) -> Target:
    """Follow the link like the updater and look at the first bytes."""
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-3"})
    try:
        resp = opener(req, timeout=30)
    except urllib.error.HTTPError as e:
        raise DownloadError(_refused(e.code, e.read(4096) if e.fp else b"")) from e
    except (urllib.error.URLError, OSError) as e:
        raise DownloadError(f"Can't reach the official server: {getattr(e, 'reason', e)}") from e
    with resp:
        status = _status(resp)
        head = resp.read(4096 if status != 206 else 4)
        if not head.startswith(b"PK"):
            raise DownloadError(_refused(status, head))
        final = resp.geturl() if hasattr(resp, "geturl") else url
        md5 = _md5_header(resp.headers.get("x-amz-meta-filemd5", ""))
        crange = resp.headers.get("Content-Range", "")
        if status == 206 and "/" in crange and crange.rsplit("/", 1)[1].isdigit():
            return Target(final, int(crange.rsplit("/", 1)[1]), md5, True)
        return Target(final, int(resp.headers.get("Content-Length") or 0), md5, False)


def _plan(size: int, ranges: bool, connections: int) -> list[list[int]]:
    """[start, end_inclusive, done] per connection."""
    n = max(1, min(connections, size // SEGMENT_MIN)) if ranges and size else 1
    if not size:
        return [[0, -1, 0]]
    step = -(-size // n)
    return [[i, min(i + step, size) - 1, 0] for i in range(0, size, step)]


def _make_sparse(fh) -> None:
    """Windows writes zeros up to any offset it hasn't seen yet unless the file is sparse."""
    from opx.backup import make_sparse

    make_sparse(fh)


class _Job:
    def __init__(self, url, out, headers, target, md5, connections, opener, cancel, stage):
        self.source = url
        self.headers = headers
        self.target = target
        self.md5 = md5
        self.opener = opener
        self.cancel = cancel or (lambda: False)
        self.stage = stage or (lambda s: None)
        self.part = out.with_name(out.name + ".part")
        self.state_file = out.with_name(out.name + ".part.json")
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error: BaseException | None = None
        self.url = target.url
        self.url_age = time.monotonic()
        self.renewals = 0
        self.segments = self._resume(connections)

    # --- resume -------------------------------------------------------------

    def _key(self) -> dict:
        return {"size": self.target.size, "md5": self.md5, "source": self.source.split("?")[0]}

    def _resume(self, connections: int) -> list[list[int]]:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if (state.get("key") == self._key() and self.part.exists()
                    and self.part.stat().st_size == self.target.size and self.target.ranges):
                segs = [list(map(int, s)) for s in state["segments"]]
                if all(0 <= s[2] <= s[1] - s[0] + 1 for s in segs):
                    done = sum(s[2] for s in segs)
                    self.stage(f"Resuming from {done / 1e6:,.0f} MB.")
                    return segs
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.part.unlink(missing_ok=True)
        with open(self.part, "wb") as fh:
            _make_sparse(fh)
            if self.target.size:
                fh.truncate(self.target.size)
        return _plan(self.target.size, self.target.ranges, connections)

    def save(self) -> None:
        if not (self.target.ranges and self.target.size):
            return
        with self.lock:
            data = {"key": self._key(), "segments": [list(s) for s in self.segments]}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.state_file)

    @property
    def done(self) -> int:
        with self.lock:
            return sum(s[2] for s in self.segments)

    # --- links --------------------------------------------------------------

    def renew(self, seen_url: str) -> str:
        """Ask the original link again for a fresh signed URL (once per expiry)."""
        with self.lock:
            if self.url != seen_url:  # another connection already renewed it
                return self.url
            if self.renewals >= 5:
                raise DownloadError("The official server keeps refusing the download link.")
            self.renewals += 1
        self.stage("The download link expired; asking the official server for a new one...")
        fresh = probe(self.source, self.headers, self.opener)
        if self.target.size and fresh.size and fresh.size != self.target.size:
            raise DownloadError("The official server now sends a different file; start the download again.")
        with self.lock:
            self.url = fresh.url
        return fresh.url

    # --- workers ------------------------------------------------------------

    def run_segment(self, seg: list[int]) -> None:
        attempts = 0
        while not self.stop.is_set():
            with self.lock:
                start, end, done = seg
            unknown = end < 0
            if not unknown and start + done > end:
                return
            url = self.url
            hdrs = dict(self.headers)
            if self.target.ranges:
                hdrs["Range"] = f"bytes={start + done}-{'' if unknown else end}"
            elif done:  # no ranges: start over
                with self.lock:
                    seg[2] = 0
            try:
                with self.opener(urllib.request.Request(url, headers=hdrs), timeout=60) as resp:
                    status = _status(resp)
                    if self.target.ranges and status != 206:
                        raise DownloadError(f"The server ignored the byte range (HTTP {status}).")
                    with open(self.part, "r+b") as fh:
                        fh.seek(start + seg[2])
                        while not self.stop.is_set():
                            want = CHUNK if unknown else min(CHUNK, end - (start + seg[2]) + 1)
                            if want <= 0:
                                return
                            chunk = resp.read(want)
                            if not chunk:
                                if unknown:
                                    return
                                raise ConnectionError("the server closed the connection early")
                            fh.write(chunk)
                            with self.lock:
                                seg[2] += len(chunk)
                            attempts = 0
                            if self.cancel():
                                self.stop.set()
                return
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404, 410):
                    self.renew(url)
                elif e.code == 416:
                    raise DownloadError("The server's file changed size; start the download again.") from e
                attempts += 1
            except DownloadError:
                raise
            except (urllib.error.URLError, OSError, ConnectionError, http.client.HTTPException) as e:
                attempts += 1
                self.stage(f"Connection problem ({getattr(e, 'reason', e)}); retrying...")
            if attempts > RETRIES:
                raise DownloadError("The download keeps failing; check your connection and start it again "
                                    "(it resumes where it stopped).")
            self.stop.wait(backoff(attempts))

    def _worker(self, seg) -> None:
        try:
            self.run_segment(seg)
        except BaseException as e:  # noqa: BLE001 (reported by the main thread)
            with self.lock:
                self.error = self.error or e
            self.stop.set()


def _verify(path: Path, md5: str, size: int, progress: Progress | None, cancel) -> None:
    if md5:
        h = hashlib.md5()
        done = 0
        with open(path, "rb") as fh:
            while chunk := fh.read(8 << 20):
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, size)
                if cancel and cancel():
                    raise Paused("Check cancelled. Start the download again to finish it.")
        if h.hexdigest().lower() != md5.lower():
            raise DownloadError("The downloaded package doesn't match its MD5, so it was deleted. Try again.")
    try:
        with zipfile.ZipFile(path) as zf:
            zf.namelist()
    except (zipfile.BadZipFile, OSError) as e:
        raise DownloadError("The downloaded file isn't a complete zip, so it was deleted. Try again.") from e


def fetch(url: str, out: Path, headers: dict[str, str], *, size: int = 0, md5: str = "",
          connections: int = CONNECTIONS, progress: Progress | None = None, stage: Stage | None = None,
          cancel: Callable[[], bool] | None = None, opener=None) -> Path:
    """Download `url` to `out` (resumable). Raises Paused when cancelled, DownloadError otherwise."""
    opener = opener or urllib.request.urlopen
    stage = stage or (lambda s: None)
    out.parent.mkdir(parents=True, exist_ok=True)
    stage("Asking the official server for the package...")
    target = probe(url, headers, opener)
    if size and target.size and size != target.size:
        raise DownloadError(f"The server's package is {target.size:,} bytes but the catalog says {size:,}.")
    target.size = target.size or size
    md5 = (md5 or target.md5).lower()
    job = _Job(url, out, headers, target, md5, connections, opener, cancel, stage)
    total = target.size
    stage(f"Downloading over {len(job.segments)} connection(s)"
          + ("" if target.ranges else " (this server can't resume)") + "...")

    threads = [threading.Thread(target=job._worker, args=(s,), daemon=True) for s in job.segments]
    for t in threads:
        t.start()
    last_save = time.monotonic()
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(0.2)
        if progress:
            progress(job.done, total)
        if cancel and cancel():
            job.stop.set()
        if time.monotonic() - last_save > SAVE_EVERY:
            job.save()
            last_save = time.monotonic()
    job.save()
    if progress:
        progress(job.done, total)

    if job.error is not None:
        if isinstance(job.error, DownloadError):
            raise job.error
        raise DownloadError(f"Download failed: {job.error}") from job.error
    if cancel and cancel() or (total and job.done < total):
        if not target.ranges:
            job.part.unlink(missing_ok=True)
            raise Paused("Download cancelled.")
        raise Paused(f"Download paused at {job.done / 1e6:,.0f} of {total / 1e6:,.0f} MB. "
                     "Start it again to resume.")

    stage("Checking the package (MD5 and zip)..." if md5 else "Checking the package (zip)...")
    try:
        _verify(job.part, md5, total, progress, cancel)
    except Paused:
        raise
    except DownloadError:
        job.part.unlink(missing_ok=True)
        job.state_file.unlink(missing_ok=True)
        raise
    os.replace(job.part, out)
    job.state_file.unlink(missing_ok=True)
    stage(f"Saved {out.name}" + (" (MD5 verified)." if md5 else "."))
    return out
