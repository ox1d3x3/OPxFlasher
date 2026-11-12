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
