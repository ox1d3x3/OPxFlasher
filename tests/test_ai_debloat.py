"""AI-assisted debloat: scan, export, import recommendations, apply with restore points; MCP server."""

import io
import json

import pytest
from test_flash import phone  # noqa: F401  (fixture)

from opx import apps as ap
from opx import mcp_server as mcp
from opx.session import open_session


@pytest.fixture
def undo(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "undo_dir", lambda: tmp_path / "debloat")


@pytest.fixture
def with_user_app(fake_device):
    pk = {p: {"removed": False, "disabled": False} for p in
          ["android", "com.android.systemui", "com.heytap.browser", "com.oplus.games", "com.coloros.weather2"]}
    pk["com.heytap.browser"]["path"] = "/my_stock/app/Browser/Browser.apk"
    pk["com.whatsapp"] = {"removed": False, "disabled": False, "user": True}
    (fake_device / "packages.json").write_text(json.dumps(pk))


def test_scan_knows_where_apps_live_and_which_are_protected(adb, phone, undo, with_user_app):
    s = open_session("FAKE0001", adb)
    apps = {a.package: a for a in ap.scan_apps(s.adb, s.profile.keep_apps)}
    assert apps["com.heytap.browser"].location == "my_stock"
    assert apps["com.oplus.games"].location == "system"
    assert apps["com.whatsapp"].user_app and apps["com.whatsapp"].location == "data"
    assert apps["com.android.systemui"].keep and not apps["com.heytap.browser"].keep


def test_export_has_no_serial_and_says_what_is_protected(adb, phone, undo, with_user_app):
    s = open_session("FAKE0001", adb)
    inv = ap.inventory(ap.scan_apps(s.adb, s.profile.keep_apps), s.props, s.access.usable)
    text = json.dumps(inv)
    assert "FAKE0001" not in text and inv["format"] == "opxflasher-apps"
    by = {a["package"]: a for a in inv["apps"]}
    assert by["com.android.systemui"]["protected"] and not by["com.heytap.browser"]["protected"]
    assert "opxflasher-recommendations" in ap.ai_prompt(s.props)


def test_recommendations_from_an_ai_reply(tmp_path):
    f = tmp_path / "reply.json"
    f.write_text("```json\n" + json.dumps({"format": "opxflasher-recommendations", "packages": [
        {"package": "com.heytap.browser", "level": "safe", "reason": "duplicate browser"},
        {"package": "com.oplus.games", "level": "AGGRESSIVE", "reason": "game space"},
        {"package": "com.coloros.weather2", "level": "nonsense"},
        "com.example.extra"]}) + "\n```")
    recs = {r.package: r for r in ap.read_recommendations(f)}
    assert recs["com.heytap.browser"].level == "safe"
    assert recs["com.oplus.games"].level == "aggressive"
    assert recs["com.coloros.weather2"].level == "keep"  # unreadable level: never a reason to remove
    assert recs["com.example.extra"].level == "recommended"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"packages": [{"package": "com.x; reboot"}]}))
    with pytest.raises(ap.AppError):
        ap.read_recommendations(bad)


def test_plan_refuses_protected_whatever_the_list_says(adb, phone, undo, with_user_app):
    s = open_session("FAKE0001", adb)
    apps = ap.scan_apps(s.adb, s.profile.keep_apps)
    recs = [ap.Recommendation("com.android.systemui", "safe", "the AI says so"),
            ap.Recommendation("com.heytap.browser", "safe"), ap.Recommendation("com.nope.missing", "safe"),
            ap.Recommendation("com.oplus.games", "keep")]
    plan = {p.package: p for p in ap.plan_recommendations(recs, apps)}
    assert plan["com.android.systemui"].status == "protected"
    assert plan["com.heytap.browser"].removable
    assert plan["com.nope.missing"].status == "not installed"
    assert plan["com.oplus.games"].status == "keep"


def test_apply_records_a_restore_point(adb, phone, undo, with_user_app):
    s = open_session("FAKE0001", adb)
    done, problems = ap.apply(s.adb, s.serial, ["com.heytap.browser", "com.android.systemui"],
                              s.profile.keep_apps, source="test")
    assert done == ["com.heytap.browser"] and problems
    runs = ap.runs(s.serial)
    assert runs[-1]["packages"] == ["com.heytap.browser"] and runs[-1]["source"] == "test"
    ap.restore(s.adb, s.serial, "com.heytap.browser")
    assert ap.runs(s.serial)  # restoring doesn't drop the history


# --- MCP ------------------------------------------------------------------------------------


def rpc(server, method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def call(server, name, **args):
    r = rpc(server, "tools/call", {"name": name, "arguments": args})["result"]
    return r, (json.loads(r["content"][0]["text"]) if not r["isError"] else r["content"][0]["text"])


def test_mcp_handshake_and_tools(adb):
    srv = mcp.Server(adb)
    init = rpc(srv, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in rpc(srv, "tools/list")["result"]["tools"]}
    assert {"scan_apps", "debloat_preview", "debloat_apply", "restore_apps"} <= names
    assert not any("flash" in n or "wipe" in n for n in names)  # debloat only
    apply_tool = next(t for t in rpc(srv, "tools/list")["result"]["tools"] if t["name"] == "debloat_apply")
    assert apply_tool["annotations"]["destructiveHint"] is True


def test_mcp_debloat_needs_a_preview_and_never_touches_protected(adb, phone, undo, with_user_app):
    srv = mcp.Server(adb)
    r, inv = call(srv, "scan_apps")
    assert not r["isError"] and any(a["package"] == "com.heytap.browser" for a in inv["apps"])
    r, err = call(srv, "debloat_apply", preview_id="made-up")
    assert r["isError"] and "preview" in err
    r, prev = call(srv, "debloat_preview", packages=[
        {"package": "com.heytap.browser", "level": "safe", "reason": "dup"},
        {"package": "com.android.systemui", "level": "safe"}])
    assert prev["will_remove"] == ["com.heytap.browser"]
    assert prev["not_removed"][0]["status"] == "protected"
    r, out = call(srv, "debloat_apply", preview_id=prev["preview_id"])
    assert out["removed"] == ["com.heytap.browser"]
    r, err = call(srv, "debloat_apply", preview_id=prev["preview_id"])  # a preview is used once
    assert r["isError"]
    r, rem = call(srv, "list_removed")
    assert rem["removed_by_opxflasher"] == ["com.heytap.browser"]
    r, back = call(srv, "restore_apps", last_run=True)
    assert back["restored"] == ["com.heytap.browser"]


def test_mcp_over_stdio(adb, phone, undo, with_user_app):
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_phones", "arguments": {}}}]
    stdin = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in lines) + b"not json\n")
    stdout = io.BytesIO()
    mcp.serve(stdin, stdout, mcp.Server(adb))
    replies = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    assert json.loads(replies[1]["result"]["content"][0]["text"])["phones"][0]["serial"] == "FAKE0001"
    assert replies[2]["error"]["code"] == -32700


def test_claude_desktop_config_is_merged_with_a_backup(tmp_path, monkeypatch):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    monkeypatch.setattr(mcp, "claude_config_path", lambda: cfg)
    path, backup = mcp.add_to_claude_desktop()
    data = json.loads(path.read_text())
    assert set(data["mcpServers"]) == {"other", "opxflasher"} and data["theme"] == "dark"
    assert data["mcpServers"]["opxflasher"]["args"][-1] == "mcp"
    assert json.loads(backup.read_text())["mcpServers"] == {"other": {"command": "x"}}
    cfg.write_text("{broken")
    with pytest.raises(ValueError):
        mcp.add_to_claude_desktop()


# --- OP Debloater ---------------------------------------------------------------------------


def test_op_debloat_files_are_what_the_module_reads(tmp_path):
    js, lst = ap.write_op_debloat(tmp_path / "x", ["com.b.two", "com.a.one", "com.a.one"], "My list",
                                  {"ro.product.model": "CPH2653"})
    data = json.loads(js.read_text())
    assert data["format"] == "op-debloat" and data["schema"] == 1
    assert data["packages"] == ["com.a.one", "com.b.two"] and data["count"] == 2  # strings, sorted, unique
    assert lst.read_text().splitlines() == ["# NAME: My list", "com.a.one", "com.b.two"]
    # and OPxFlasher reads the module's files back
    assert [r.package for r in ap.read_recommendations(js)] == ["com.a.one", "com.b.two"]
    assert ap.read_list(lst) == ["com.a.one", "com.b.two"]


def test_send_to_op_debloat_copies_and_can_run(adb, phone, undo, with_user_app, fake_device, tmp_path,
                                                monkeypatch):
    s = open_session("FAKE0001", adb)
    assert ap.op_debloater_status(s.adb) == "missing" and not ap.op_debloater_installed(s.adb)
    out = ap.send_to_op_debloat(s.adb, ["com.heytap.browser"], "OPxFlasher picks", s.props, tmp_path)
    sent = json.loads((fake_device / "sdcard" / out["json"][len("/sdcard/"):]).read_text())
    assert sent["packages"] == ["com.heytap.browser"] and not out["ran"]
    assert (fake_device / "sdcard" / "debloater" / "profiles" / "OPxFlasher_picks.list").exists()
    assert out["profile"] == "custom:OPxFlasher_picks"
    with pytest.raises(ap.AppError, match="isn't installed"):  # never "removed" when nothing ran
        ap.send_to_op_debloat(s.adb, ["com.heytap.browser"], "x", s.props, tmp_path, run=True)
    monkeypatch.setenv("FAKE_OP_DEBLOAT", "1")
    assert ap.op_debloater_installed(s.adb)
    monkeypatch.setenv("FAKE_UNINSTALL_REFUSED", "com.oplus.games")
    out = ap.send_to_op_debloat(s.adb, ["com.heytap.browser", "com.oplus.games", "com.android.systemui",
                                        "com.nope.missing"], "OPxFlasher picks", s.props, tmp_path, run=True)
    assert out["ran"] and out["summary"] == {"ok": 2, "skip": 2, "fail": 0}
    assert out["removed"] == ["com.heytap.browser", "com.oplus.games"]
    assert any(x.startswith("com.android.systemui: protected") for x in out["skipped"])
    pk = json.loads((fake_device / "packages.json").read_text())
    assert pk["com.heytap.browser"]["removed"] and pk["com.oplus.games"]["disabled"]  # the module's fallback
    # the engine itself, not /system/bin/debloater (only mounted on Magisk / KSU with a mount metamodule)
    log = (tmp_path / "commands.log").read_text()
    assert "sh /data/adb/modules/debloater_systemless_x1/bin/debloater -m remove -f /data/media/0/debloater/" \
        "import/" in log
    back = ap.op_debloat_restore(s.adb)  # the module's undo
    assert back["summary"]["ok"] == 2
    assert not json.loads((fake_device / "packages.json").read_text())["com.heytap.browser"]["removed"]


def test_op_debloater_disabled_or_no_root(adb, phone, undo, with_user_app, tmp_path, monkeypatch):
    s = open_session("FAKE0001", adb)
    monkeypatch.setenv("FAKE_OP_DEBLOAT", "disabled")
    assert ap.op_debloater_status(s.adb) == "disabled"
    with pytest.raises(ap.AppError, match="disabled"):
        ap.send_to_op_debloat(s.adb, ["com.heytap.browser"], "x", s.props, tmp_path, run=True)


def test_op_machine_output():
    out = ap.parse_op_machine("T\t2\tx\nR\ta.b\tok\tuninstalled\t\nR\tc.d\tfail\t-\tboom\nS\t1\t0\t1\n")
    assert out["summary"] == {"ok": 1, "skip": 0, "fail": 1} and out["results"][1]["message"] == "boom"
    assert ap.parse_op_machine("E\tRun as root (su).\n")["error"] == "Run as root (su)."


def test_remove_falls_back_like_the_module(adb, phone, undo, with_user_app, fake_device, monkeypatch):
    # ColorOS refuses `pm uninstall` for some system apps: disable, then suspend, instead of failing
    monkeypatch.setenv("FAKE_UNINSTALL_REFUSED", "com.oplus.games,com.coloros.weather2")
    monkeypatch.setenv("FAKE_DISABLE_REFUSED", "com.coloros.weather2")
    s = open_session("FAKE0001", adb)
    assert ap.remove(s.adb, s.serial, "com.heytap.browser") == "uninstalled"
    assert ap.remove(s.adb, s.serial, "com.oplus.games") == "disabled"
    assert ap.remove(s.adb, s.serial, "com.coloros.weather2") == "suspended"
    pk = json.loads((fake_device / "packages.json").read_text())
    assert pk["com.oplus.games"]["disabled"] and pk["com.coloros.weather2"]["suspended"]
    monkeypatch.setenv("FAKE_UNINSTALL_REFUSED", "com.oplus.games,com.coloros.weather2,com.whatsapp")
    monkeypatch.setenv("FAKE_DISABLE_REFUSED", "com.whatsapp")
    monkeypatch.setenv("FAKE_SUSPEND_REFUSED", "com.whatsapp")
    with pytest.raises(ap.AppError, match="DELETE_FAILED_INTERNAL_ERROR"):  # the phone's own words
        ap.remove(s.adb, s.serial, "com.whatsapp")
    for p in ("com.heytap.browser", "com.oplus.games", "com.coloros.weather2"):
        ap.restore(s.adb, s.serial, p)
    pk = json.loads((fake_device / "packages.json").read_text())
    assert not any(pk[p]["removed"] or pk[p]["disabled"] or pk[p].get("suspended")
                   for p in ("com.heytap.browser", "com.oplus.games", "com.coloros.weather2"))
    assert ap.removed_by_us(s.serial) == []


def test_mcp_send_to_op_debloater(adb, phone, undo, with_user_app, fake_device, monkeypatch):
    srv = mcp.Server(adb)
    r, prev = call(srv, "debloat_preview", packages=["com.heytap.browser"])
    r, err = call(srv, "send_to_op_debloater", preview_id=prev["preview_id"], run=True)
    assert r["isError"] and "isn't installed" in err
    monkeypatch.setenv("FAKE_OP_DEBLOAT", "1")
    r, out = call(srv, "send_to_op_debloater", preview_id=prev["preview_id"], run=True)
    assert out["ran"] and out["removed"] == ["com.heytap.browser"] and "undo" in out
    assert json.loads((fake_device / "packages.json").read_text())["com.heytap.browser"]["removed"]
