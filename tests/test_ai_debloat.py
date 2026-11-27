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
