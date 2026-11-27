

def test_custom_recovery_is_known_by_its_board():
    # AERA on a real OnePlus 13: model "OnePlus 13", product "ossi", board "dodge"
    from opx import profile as profiles

    props = {"ro.product.model": "OnePlus 13", "ro.product.device": "ossi", "ro.product.vendor.device": "dodge",
             "ro.build.product": "dodge"}
    assert profiles.detect(props).id == "oneplus-13"
    assert profiles.detect({"ro.product.model": "X", "ro.product.device": "ossi"}).id == "oplus-generic"
