"""Every bundled profile is valid, and user profiles can't weaken safety."""

import json

import pytest

from opx import profile as profiles


@pytest.mark.parametrize("pid", sorted(profiles._builtin_ids()))
def test_bundled_profiles_are_valid(pid):
    raw = profiles._read(pid)
    assert raw["id"] == pid
    assert profiles.validate(raw, builtin=True) == []
    p = profiles.load(pid)
    for must in ("persist*", "modemst1", "devinfo", "userdata", "metadata", "frp"):
        assert p.is_never_flash(must.rstrip("*"))
    if pid != profiles.GENERIC:
        for model in p.match.get("ro.product.model", []):
            assert profiles.detect({"ro.product.model": model}).id == pid


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    monkeypatch.setattr(profiles, "user_profiles_dir", lambda: d)
    return d


GOOD = {"id": "oneplus-12", "name": "OnePlus 12", "inherits": "oplus-generic",
        "match": {"ro.product.model": ["CPH2581"]}, "variants": {"CPH2581": "EU"}}


def write(d, raw, name=None):
    d.mkdir(exist_ok=True)
    f = d / f"{name or raw['id']}.json"
    f.write_text(json.dumps(raw))
    return f


def test_user_profile_is_detected(user_dir):
    write(user_dir, GOOD)
    p = profiles.detect({"ro.product.model": "CPH2581"})
    assert p.id == "oneplus-12" and p.variant_name("CPH2581") == "OnePlus 12 EU (CPH2581)"
    assert p.is_never_flash("persist") and p.is_never_flash("userdata")


def test_child_cannot_drop_never_flash(user_dir):
    write(user_dir, {**GOOD, "never_flash": ["my_extra"]})
    p = profiles.load("oneplus-12")
    assert p.is_never_flash("persist") and p.is_never_flash("modemst1") and p.is_never_flash("my_extra")


def test_user_files_never_replace_builtins(user_dir):
    write(user_dir, {**GOOD, "id": "oneplus-13", "match": {"ro.product.model": ["PJZ110"]},
                     "never_flash": []}, "oneplus-13")
    assert profiles.load("oneplus-13").name == "OnePlus 13"  # the bundled one


@pytest.mark.parametrize("bad,why", [
    ({**GOOD, "inherits": "something"}, "inherit"),
    ({**GOOD, "match": {}}, "match"),
    ({**GOOD, "id": "Bad Id"}, "id must"),
    ({**GOOD, "qcom_arb_validated": True}, "qcom_arb_validated"),
    ({**GOOD, "known_issues": [{"match": {}, "message": "x", "blocks": ["edl"]}]}, "blocks"),
    ({**GOOD, "surprise": 1}, "unknown keys"),
    ({**GOOD, "never_flash": ["../etc"]}, "bad partition"),
    ({**GOOD, "codenames": ["OP5D0DL1", "bad name!"]}, "codenames"),
])
def test_invalid_profiles_are_rejected_and_ignored(user_dir, bad, why):
    f = write(user_dir, bad, "candidate")
    assert any(why in e for e in profiles.check_file(f))
    assert "candidate" not in profiles.all_profile_ids()


def test_cli_profile_commands(user_dir, tmp_path, capsys):
    from opx import cli

    f = tmp_path / "oneplus-12.json"
    f.write_text(json.dumps(GOOD))
    assert cli.main(["profile", "check", str(f)]) == 0
    assert cli.main(["profile", "install", str(f)]) == 0
    assert (user_dir / "oneplus-12.json").exists()
    assert cli.main(["profile", "list"]) == 0
    out = capsys.readouterr().out
    assert "oneplus-12" in out and "user" in out and "oneplus-13" in out
    f.write_text(json.dumps({**GOOD, "id": "oneplus-13"}))
    assert cli.main(["profile", "install", str(f)]) == 3


def test_one_phone_with_several_region_codenames():
    p = profiles.load("oneplus-13")
    assert p.same_phone("OP5D0DL1", "OP5D55L1") and p.same_phone("op5d55l1", "OP5D0DL1")
    assert p.same_phone("x", "x") and not p.same_phone("OP5D0DL1", "OP5D77L1") and not p.same_phone("", "OP5D0DL1")
    assert not profiles.load("oneplus-15").same_phone("OP5D0DL1", "OP5D55L1")


def test_ota_products_with_a_region_suffix_find_their_phone():
    """Real OnePlus 13 India OTAs say CPH2649IN; EU ones can say CPH2653EEA."""
    assert profiles.for_product("CPH2649IN").id == "oneplus-13"
    assert profiles.for_product("CPH2653EEA").id == "oneplus-13"
    assert profiles.for_product("PJZ110").id == "oneplus-13"
    assert profiles.for_product("CPH9999").id == "oplus-generic"
    assert profiles.for_product("").id == "oplus-generic"
