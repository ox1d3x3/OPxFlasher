"""OTA downloads against a simulated OPPO / OnePlus server:

- refuses anything that isn't the phone's updater (error 2306),
- `downloadCheck` redirects to a signed CDN link that expires,
- the CDN serves byte ranges and sometimes drops a connection,
plus one run through real urllib and a local HTTP server.
"""

import hashlib
import http.server
import io
import json
import os
import threading
import urllib.error
import zipfile

import pytest

from opx import catalog, otadl

CHECK = "https://component-ota-cn.allawntech.com/downloadCheck?id=abc"
CDN = "https://gauss-componentotacostmanual-cn.allawnfs.com/remove-x/ota.zip"


def make_zip(size=600_000) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("META-INF/com/android/metadata", "ota-type=AB\n")
        zf.writestr("payload.bin", os.urandom(size))
    return buf.getvalue()


PKG = make_zip()
MD5 = hashlib.md5(PKG).hexdigest()


class Resp(io.BytesIO):
    def __init__(self, body, status, headers, url, drop_after=None):
        super().__init__(body)
        self.status, self.headers, self.url, self.drop_after = status, headers, url, drop_after
        self.sent = 0

    def geturl(self):
        return self.url

    def read(self, n=-1):
        if self.drop_after is not None and self.sent >= self.drop_after:
            raise ConnectionResetError("connection reset by peer")
        data = super().read(n)
        self.sent += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class OplusServer:
    """Behaves like urlopen against OPPO's servers (redirects followed, errors raised)."""

    def __init__(self, body=PKG, ranges=True, ttl=10**9, drops=0, md5_header=True, refuse_always=False):
        self.body, self.ranges, self.ttl, self.drops = body, ranges, ttl, drops
        self.md5_header, self.refuse_always = md5_header, refuse_always
        self.token = 0
        self.uses = 0
        self.requests = []
        self.lock = threading.Lock()

    def __call__(self, req, timeout=None):
        with self.lock:
            self.requests.append(req)
            url = req.full_url
            if url.startswith(CHECK):
                updater = (req.get_header("User-agent") or "").startswith("Dalvik/") and req.get_header("Userid")
                if self.refuse_always or not updater:
                    return Resp(json.dumps({"code": 2306, "msg": "illegal request"}).encode(), 200, {}, url)
                self.token += 1
                self.uses = 0
                url = f"{CDN}?sign={self.token}&Expires=9999"  # 302 followed, like urlopen
            if url.startswith(CDN):
                if f"sign={self.token}" not in url or self.uses >= self.ttl:
                    raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"<Error>AccessDenied"))
                self.uses += 1
                drop = None
                if self.drops > 0 and req.get_header("Range") not in (None, "bytes=0-3"):
                    self.drops -= 1
                    drop = 50_000
                headers = {"x-amz-meta-filemd5": MD5} if self.md5_header else {}
                rng = req.get_header("Range")
                if self.ranges and rng:
                    start, _, end = rng.split("=")[1].partition("-")
                    start, end = int(start), int(end or len(self.body) - 1)
                    part = self.body[start:end + 1]
                    headers |= {"Content-Range": f"bytes {start}-{end}/{len(self.body)}",
                                "Content-Length": str(len(part))}
                    return Resp(part, 206, headers, url, drop)
                headers["Content-Length"] = str(len(self.body))
                return Resp(self.body, 200, headers, url, drop)
            raise urllib.error.URLError("unknown host")

    def ranges_asked(self):
        return [r.get_header("Range") for r in self.requests if r.full_url.startswith(CDN)]


@pytest.fixture(autouse=True)
def small_and_fast(monkeypatch):
    monkeypatch.setattr(otadl, "SEGMENT_MIN", 64 * 1024)
    monkeypatch.setattr(otadl, "CHUNK", 16 * 1024)
    monkeypatch.setattr(otadl, "backoff", lambda attempt: 0)


def headers():
    return otadl.updater_headers("PKJ110", "16")


def test_updater_headers():
    h = otadl.updater_headers("PJZ110", "16", "BP2A.250605.015")
    assert h["User-Agent"] == "Dalvik/2.1.0 (Linux; U; Android 16; PJZ110 Build/BP2A.250605.015)"
    assert h["userId"] == "oplus-ota|16002018"
    assert otadl.updater_headers()["User-Agent"] == "Dalvik/2.1.0 (Linux; U; Android 16)"


def test_plain_requests_are_refused_like_before(tmp_path):
    srv = OplusServer()
    with pytest.raises(otadl.DownloadError, match="refused the download.*error 2306"):
        otadl.fetch(CHECK, tmp_path / "ota.zip", {"User-Agent": "OPxFlasher/0.7.0"}, opener=srv)


def test_downloads_like_the_updater_over_several_connections(tmp_path):
    srv = OplusServer()
    stages = []
    seen = []
    out = otadl.fetch(CHECK, tmp_path / "ota.zip", headers(), opener=srv, stage=stages.append,
                      progress=lambda d, t: seen.append((d, t)))
    assert out.read_bytes() == PKG
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ota.zip"]  # no .part / .json left
    starts = {r for r in srv.ranges_asked() if r != "bytes=0-3"}
    assert len(starts) == 4  # four connections
    assert all(r.get_header("Userid") == otadl.UPDATER_USER_ID for r in srv.requests)
    assert any("MD5" in s for s in stages) and stages[-1].endswith("(MD5 verified).")
    assert seen[-1] == (len(PKG), len(PKG))


def test_expired_link_is_renewed(tmp_path):
    srv = OplusServer(ttl=6)  # the signed link stops working after a few requests
    out = otadl.fetch(CHECK, tmp_path / "ota.zip", headers(), opener=srv, connections=8)
    assert out.read_bytes() == PKG and srv.token >= 2


def test_dropped_connections_are_retried(tmp_path):
    srv = OplusServer(drops=3)
    stages = []
    out = otadl.fetch(CHECK, tmp_path / "ota.zip", headers(), opener=srv, stage=stages.append)
    assert out.read_bytes() == PKG
    assert any("retrying" in s for s in stages)


def test_pause_and_resume(tmp_path):
    srv = OplusServer()
    calls = {"n": 0}

    def cancel_soon():
        calls["n"] += 1
        return calls["n"] > 6

    with pytest.raises(otadl.Paused, match="Start it again to resume"):
        otadl.fetch(CHECK, tmp_path / "ota.zip", headers(), opener=srv, cancel=cancel_soon, connections=1)
    assert (tmp_path / "ota.zip.part").exists() and (tmp_path / "ota.zip.part.json").exists()
    done_before = sum(s[2] for s in json.loads((tmp_path / "ota.zip.part.json").read_text())["segments"])
    assert 0 < done_before < len(PKG)

    srv2 = OplusServer()
    stages = []
    out = otadl.fetch(CHECK, tmp_path / "ota.zip", headers(), opener=srv2, stage=stages.append, connections=1)
    assert out.read_bytes() == PKG
    assert any(s.startswith("Resuming from") for s in stages)
    first = [r for r in srv2.ranges_asked() if r != "bytes=0-3"][0]
    assert first == f"bytes={done_before}-{len(PKG) - 1}"  # carried on, didn't start over


def test_md5_mismatch_deletes_everything(tmp_path):
    srv = OplusServer(md5_header=False)
    with pytest.raises(otadl.DownloadError, match="doesn't match its MD5"):
        otadl.fetch(CHECK, tmp_path / "ota.zip", headers(), md5="0" * 32, opener=srv)
    assert list(tmp_path.iterdir()) == []


def test_size_mismatch_with_catalog(tmp_path):
    with pytest.raises(otadl.DownloadError, match="catalog says"):
        otadl.fetch(CHECK, tmp_path / "ota.zip", headers(), size=123, opener=OplusServer())


def test_server_without_ranges_uses_one_connection(tmp_path):
    srv = OplusServer(ranges=False)
    stages = []
    out = otadl.fetch(CHECK, tmp_path / "ota.zip", headers(), opener=srv, stage=stages.append)
    assert out.read_bytes() == PKG and any("can't resume" in s for s in stages)
    with pytest.raises(otadl.Paused, match="cancelled"):
        otadl.fetch(CHECK, tmp_path / "again.zip", headers(), opener=OplusServer(ranges=False),
                    cancel=lambda: True)
    assert not (tmp_path / "again.zip.part").exists()


def test_md5_header_formats():
    raw = hashlib.md5(b"x").digest()
    assert otadl._md5_header(raw.hex().upper()) == raw.hex()
    assert otadl._md5_header(__import__("base64").b64encode(raw).decode()) == raw.hex()
    assert otadl._md5_header("not-an-md5") == ""


def test_catalog_download_uses_the_updater_identity(tmp_path):
    srv = OplusServer()
    rel = catalog.Release(id="x", device="Find X9 Ultra", region="CN", model="PKJ110",
                          version="16.0.10.501(CN01)", source_url=CHECK, size=len(PKG), md5=MD5)
    out = catalog.download(rel, tmp_path, opener=srv)
    assert out.read_bytes() == PKG
    assert srv.requests[0].get_header("User-agent").startswith("Dalvik/2.1.0 (Linux; U; Android 16; PKJ110")
    with pytest.raises(catalog.CatalogError, match="error 2306"):
        catalog.download(rel, tmp_path / "x", opener=OplusServer(refuse_always=True))


def test_real_http_redirect_keeps_updater_headers(tmp_path, monkeypatch):
    """Real urllib + a local HTTP server: the redirect keeps userId, ranges work."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/downloadCheck"):
                if self.headers.get("userId") != otadl.UPDATER_USER_ID:
                    self._send(200, b'{"code":2306}')
                    return
                self.send_response(302)
                self.send_header("Location", "/cdn/ota.zip?sign=1&Expires=9999")
                self.end_headers()
                return
            if self.headers.get("userId") != otadl.UPDATER_USER_ID:  # forwarded through the redirect
                self._send(403, b"denied")
                return
            start, _, end = self.headers["Range"].split("=")[1].partition("-")
            start, end = int(start), int(end or len(PKG) - 1)
            body = PKG[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(PKG)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/downloadCheck?id=1"
        out = otadl.fetch(url, tmp_path / "ota.zip", headers(), md5=MD5)
        assert out.read_bytes() == PKG
    finally:
        srv.shutdown()
