"""Built-in debloat lists and the OPPO protected-app patterns."""

import pytest

from opx import apps as ap


def test_builtin_lists_are_valid_and_never_touch_protected_apps():
    from opx import profile

    lists = ap.builtin_lists()
    assert {p.name for _t, _d, p in lists} >= {"coloros16-cn-1-minimal.txt", "coloros16-cn-2-recommended.txt",
                                              "coloros16-cn-3-aggressive.txt", "google-extras.txt"}
    keep = profile.load("oneplus-13").keep_apps
    for title, about, path in lists:
        pkgs = ap.read_list(path)  # raises on anything that isn't a package name
        assert pkgs and title and about, path
        assert not [p for p in pkgs if ap.keep_reason(p, keep)], path
    minimal = set(ap.read_list(next(p for t, _d, p in lists if "Minimal" in t)))
    rec = set(ap.read_list(next(p for t, _d, p in lists if "Recommended" in t)))
    agg = set(ap.read_list(next(p for t, _d, p in lists if "Aggressive" in t)))
    assert minimal <= rec <= agg  # each level includes the one before


@pytest.mark.parametrize(("pkg", "kept"), [
    ("com.oplus.athena", True), ("com.google.android.gms", True), ("com.android.webview", True),
    ("com.topjohnwu.magisk", True), ("com.oplus.framework.x", True),
    ("com.android.systemui.overlay.thing", True),  # *overlay*
    ("vendor.foo_rro", True),  # *_rro
    ("com.opos.ads", False), ("com.heytap.browser", False),
])
def test_oppo_protected_apps(pkg, kept):
    from opx import profile

    assert bool(ap.keep_reason(pkg, profile.load("oneplus-13").keep_apps)) == kept
