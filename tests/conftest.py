import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from opx.tools import Tool  # noqa: E402

FAKE = Path(__file__).with_name("fake_adb.py")

PROPS = {
    "ro.product.model": "PJZ110",
    "ro.product.device": "testdevice",
    "ro.product.name": "PJZ110",
    "ro.build.display.id": "PJZ110_16.0.3.501(CN01)",
    "ro.build.fingerprint": "OnePlus/PJZ110/testdevice:16/test:user/release-keys",
    "ro.build.version.security_patch": "2026-09-01",
    "ro.build.version.release": "16",
    "ro.boot.slot_suffix": "_a",
    "ro.boot.verifiedbootstate": "orange",
    "ro.serialno": "FAKE0001",
}

PHYSICAL = {
    "oplusstanvbk_a": 4096 * 3,
    "oplusstanvbk_b": 4096 * 3,
    "persist": 8192,
    "modemst1": 4096,
    "modemst2": 4096,
    "fsg": 4096,
    "fsc": 512,
    "ocdt": 1024,
    "devinfo": 512,
    "oplusreserve1": 2048,
    "oplusreserve2": 2048,
    "boot_a": 65536,
    "boot_b": 65536,
    "super": 131072,
    "userdata": 262144,
}

LOGICAL = {"my_region_a": 16384, "system_a": 32768}


@pytest.fixture
def fake_device(tmp_path, monkeypatch):
    root = tmp_path / "phone"
    (root / "by-name").mkdir(parents=True)
    (root / "mapper").mkdir()
    for folder, table in (("by-name", PHYSICAL), ("mapper", LOGICAL)):
        for name, size in table.items():
            # distinct, deterministic content per partition
            seed = name.encode()
            data = (seed * (size // len(seed) + 1))[:size]
            (root / folder / name).write_bytes(data)
    (root / "mapper" / "control").write_bytes(b"")
    (root / "props.json").write_text(json.dumps(PROPS))
    monkeypatch.setenv("FAKE_DEVICE_DIR", str(root))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "commands.log"))
    monkeypatch.setenv("FAKE_ACCESS", "su")
    return root


@pytest.fixture
def adb(fake_device):
    return Tool("adb", prefix=[sys.executable, str(FAKE)])


@pytest.fixture
def adb_env(fake_device, monkeypatch):
    """Point opx.tools.find_tool at the fake adb via the environment."""
    monkeypatch.setenv("OPX_ADB", f'"{sys.executable}" "{FAKE}"' if os.name == "nt" else f"{sys.executable} {FAKE}")
    monkeypatch.setenv("OPX_FASTBOOT", f"{sys.executable} -c pass")
    return fake_device


FAKE_FASTBOOT = Path(__file__).with_name("fake_fastboot.py")


@pytest.fixture
def fb_state(fake_device, monkeypatch):
    """Shared bootloader state for fake_adb / fake_fastboot (phone starts in Android)."""
    monkeypatch.setenv("OPX_POLL_SECONDS", "0.01")
    path = fake_device / "fastboot.json"

    def update(**changes):
        state = json.loads(path.read_text())
        state.update(changes)
        path.write_text(json.dumps(state))
        return state

    path.write_text(json.dumps({
        "mode": "android", "serial": "FAKE0001", "slot": "a", "unlocked": True,
        "super_size": 1 << 20, "fastbootd_broken": False, "fail_flash": None, "log": [],
    }))
    update.path = path
    update.read = lambda: json.loads(path.read_text())
    return update


@pytest.fixture
def fastboot(fb_state):
    return Tool("fastboot", prefix=[sys.executable, str(FAKE_FASTBOOT)])
