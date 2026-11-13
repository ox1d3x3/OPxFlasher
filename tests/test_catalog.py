"""OTA catalog client against a recorded response and a fake download server."""

import hashlib
import io
import json
import urllib.error
import zipfile

import pytest

from opx import catalog


def make_zip(payload: bytes = b"fake ota" * 1000) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("META-INF/com/android/metadata", "ota-type=AB\n")
        zf.writestr("payload.bin", payload)
    return buf.getvalue()


PKG = make_zip()
SAMPLE = {
    "api_version": "1",
    "count": 3,
    "releases": [
        {"id": "aaa111", "device": "OP 13", "region": "CN", "model": "PJZ110",
         "version": "16.0.3.501(CN01)", "ota_version": "PJZ110_11.F.20_3200_202509010000",
         "build_timestamp": "2025-09-01T00:00:00", "security_patch": "2025-09-01",
         "source_url": "https://ota.example/old.zip", "changelog_url": "", "is_latest": False},
        {"id": "bbb222", "device": "OP 13", "region": "CN", "model": "PJZ110",
         "version": "16.0.5.700(CN01)", "ota_version": "PJZ110_11.F.70_5700_202610010000",
         "build_timestamp": "2026-10-01T00:00:00", "security_patch": "2026-10-01",
         "md5": hashlib.md5(PKG).hexdigest(), "size": len(PKG),
         "source_url": "https://ota.example/new.zip", "is_latest": True, "new_field": 1},
        {"id": "ccc333", "device": "OP 13", "region": "EU", "model": "CPH2653",
         "version": "16.0.5.700(EX01)", "build_timestamp": "2026-10-02T00:00:00",
         "source_url": "https://ota.example/eu.zip", "is_latest": True},
    ],
}


class Resp(io.BytesIO):
    def __init__(self, body, headers=None, status=200, url=""):
        super().__init__(body)
        self.headers = headers or {}
        self.status = status
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Server:
    def __init__(self, files=None, etag='"v1"'):
        self.files = files or {}
        self.etag = etag
        self.requests = []
        self.offline = False

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.offline:
            raise urllib.error.URLError("no network")
        url = req.full_url
        if url.startswith(catalog.API):
            if req.headers.get("If-none-match") == self.etag:
                raise urllib.error.HTTPError(url, 304, "Not Modified", {}, None)
            return Resp(json.dumps(SAMPLE).encode(), {"ETag": self.etag})
        return Resp(self.files[url], {"Content-Length": str(len(self.files[url]))}, url=url)


def test_fetch_parses_and_sends_filters_and_user_agent(tmp_path):
    srv = Server()
    rels = catalog.fetch(model="PJZ110", latest=True, cache=tmp_path, opener=srv)
    assert [r.id for r in rels] == ["aaa111", "bbb222", "ccc333"]
    assert rels[1].size == len(PKG) and rels[1].is_latest and rels[1].extra == {"new_field": 1}
    req = srv.requests[0]
    assert "model=PJZ110" in req.full_url and "latest=1" in req.full_url
    assert req.get_header("User-agent").startswith("OPxFlasher/")


def test_fetch_revalidates_with_etag_and_works_offline(tmp_path):
    srv = Server()
    catalog.fetch(model="PJZ110", cache=tmp_path, opener=srv)
    again = catalog.fetch(model="PJZ110", cache=tmp_path, opener=srv)  # 304 -> cached copy
    assert srv.requests[1].get_header("If-none-match") == '"v1"'
    assert len(again) == 3
    srv.offline = True
    assert len(catalog.fetch(model="PJZ110", cache=tmp_path, opener=srv)) == 3


def test_fetch_offline_without_cache_is_a_clear_error(tmp_path):
    srv = Server()
    srv.offline = True
    with pytest.raises(catalog.CatalogError, match="Can't reach"):
        catalog.fetch(model="PJZ110", cache=tmp_path, opener=srv)


def test_for_phone_needs_a_model():
    with pytest.raises(catalog.CatalogError, match="model"):
        catalog.for_phone("")


def rel(i):
    return catalog.Release.from_json(SAMPLE["releases"][i])


def test_download_checks_md5_and_size(tmp_path):
    srv = Server({"https://ota.example/new.zip": PKG})
    seen = []
    out = catalog.download(rel(1), tmp_path, progress=lambda d, t: seen.append((d, t)), opener=srv)
    assert out.read_bytes() == PKG and out.name.endswith(".zip")
    assert seen[-1] == (len(PKG), len(PKG))


def test_download_rejects_wrong_md5_and_cleans_up(tmp_path):
    bad = PKG[:-1] + b"X"
    srv = Server({"https://ota.example/new.zip": bad})
    with pytest.raises(catalog.CatalogError, match="MD5"):
        catalog.download(rel(1), tmp_path, opener=srv)
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_a_page_that_is_not_a_zip(tmp_path):
    srv = Server({"https://ota.example/old.zip": b"<html>link expired</html>"})
    with pytest.raises(catalog.CatalogError, match="didn't send a zip"):
        catalog.download(rel(0), tmp_path, opener=srv)
    assert list(tmp_path.iterdir()) == []


def test_download_can_be_cancelled(tmp_path):
    srv = Server({"https://ota.example/new.zip": PKG})
    with pytest.raises(catalog.CatalogError, match="cancelled"):
        catalog.download(rel(1), tmp_path, cancel=lambda: True, opener=srv)
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_insecure_links(tmp_path):
    r = rel(1)
    r.source_url = "http://ota.example/new.zip"
    with pytest.raises(catalog.CatalogError, match="secure"):
        catalog.download(r, tmp_path, opener=Server())


def test_cli_ota_list_and_download(tmp_path, monkeypatch, capsys):
    from opx import cli

    srv = Server({"https://ota.example/new.zip": PKG})
    monkeypatch.setattr(catalog, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(catalog.urllib.request, "urlopen", srv)
    assert cli.main(["ota", "list", "--model", "PJZ110"]) == 0
    out = capsys.readouterr().out
    assert "16.0.5.700(CN01)" in out and "(latest)" in out and "Daniel Springer" in out
    assert cli.main(["ota", "download", "--model", "PJZ110", "--id", "bbb222", "--out", str(tmp_path)]) == 0
    assert "MD5 verified" in capsys.readouterr().out
    assert (tmp_path / rel(1).filename).read_bytes() == PKG
