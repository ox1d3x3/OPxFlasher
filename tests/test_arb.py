"""Validate ROM / ARB: the ROM's build against the installed one, remembered per phone."""

from types import SimpleNamespace

from opx import arb, settings


def rom(version, ts=""):
    return SimpleNamespace(path=SimpleNamespace(name="CPH2653-fullrom-GLO"),
                           metadata={"post-display-id": version, "post-timestamp": ts})


ANDROID = {"ro.build.display.id": "CPH2653_16.0.10.501(EX01)", "ro.serialno": "SER1", "ro.bootmode": "normal"}


def test_same_newer_older():
    assert arb.validate(rom("CPH2653_16.0.10.501(EX01)"), ANDROID, "SER1").result == arb.SAME
    assert arb.validate(rom("CPH2653_16.0.10.600(EX01)"), ANDROID, "SER1").result == arb.NEWER
    v = arb.validate(rom("CPH2653_16.0.7.201(EX01)"), ANDROID, "SER1")
    assert v.result == arb.OLDER and not v.ok and "DON'T flash" in v.detail


def test_recovery_props_are_never_the_installed_build():
    aera = {"ro.build.display.id": "aera_dodge-eng 99.87.36"}
    v = arb.validate(rom("CPH2653_16.0.10.501(EX01)"), aera, "SER1")
    assert v.result == arb.UNKNOWN and "Start it in Android" in v.detail


def test_unknown_versions_say_so():
    v = arb.validate(rom("custom"), {"ro.build.display.id": "custom"}, "SER1")
    assert v.result == arb.UNKNOWN


def test_saved_per_phone_and_rom(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "path", lambda: tmp_path / "settings.json")
    v = arb.validate(rom("CPH2653_16.0.10.501(EX01)"), ANDROID, "SER1")
    arb.save(v)
    got = arb.saved_for("SER1", "CPH2653-fullrom-GLO")
    assert got is not None and got.result == arb.SAME and got.checked_utc
    assert arb.saved_for("OTHER", "CPH2653-fullrom-GLO") is None
