"""Known-problem hints from saved phone logs."""

import zipfile

from opx import bootdiag as bd
from opx import cli


def logs(**files):
    return {name.replace("__", "/").replace("_txt", ".txt"): text for name, text in files.items()}


def ids(findings):
    return [f.id for f in findings]


def test_finds_known_problems_in_order():
    found = bd.analyze({
        "dmesg.txt": "[ 1.0] ok\n[ 5.2] Kernel panic - not syncing: Fatal exception\n",
        "logcat.txt": "E init: Service 'zygote' (pid 812) received signal 6\n" * 3
                      + "E snapuserd: merge failed\n",
        "getprop.txt": "[sys.boot.reason]: [kernel_panic]\n",
        "README.txt": "Kernel panic - not syncing (mentioned in the readme, must be ignored)\n",
    })
    assert ids(found)[:2] == ["panic", "zygote"]
    zyg = next(f for f in found if f.id == "zygote")
    assert zyg.count == 3 and zyg.source == "logcat.txt" and "signal 6" in zyg.evidence
    reason = next(f for f in found if f.id == "boot-reason")
    assert reason.severity == "warning" and "kernel panic" in reason.title
    assert ids(found).index("ota-merge") > ids(found).index("zygote")
    assert "Kernel panic" in bd.report(found) and "(+2 more)" in bd.report(found)


def test_other_signatures():
    cases = {
        "verity": "device-mapper: verity: dm-verity device corrupted",
        "first-stage-mount": "init: Failed to mount required partitions early ...",
        "data": "vold: Failed to mount /data: Invalid argument",
        "system-server": "*** FATAL EXCEPTION IN SYSTEM PROCESS: main",
        "modem": "subsys-restart: modem crashed",
    }
    for rule, line in cases.items():
        assert rule in ids(bd.analyze({"x.txt": line})), rule


def test_clean_logs():
    assert bd.analyze({"logcat.txt": "I ActivityManager: Start proc\n"}) == []
    assert "No known problem messages" in bd.report([])


def test_read_zip_and_folder(tmp_path):
    z = tmp_path / "logs.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("dmesg.txt", "Kernel panic - not syncing\n")
        zf.writestr("pstore/console-ramoops-0", "AVB: avb_slot_verify ERROR\n")
    assert set(ids(bd.analyze(bd.read_logs(z)))) == {"panic", "verity"}
    d = tmp_path / "dir"
    (d / "pstore").mkdir(parents=True)
    (d / "pstore" / "x").write_text("Kernel panic - not syncing\n")
    assert ids(bd.analyze(bd.read_logs(d))) == ["panic"]


def test_cli_read(tmp_path, capsys):
    z = tmp_path / "logs.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("logcat.txt", "FATAL EXCEPTION IN SYSTEM PROCESS\n")
    assert cli.main(["logs", "--read", str(z)]) == 0
    assert "system process crashed" in capsys.readouterr().out
    (tmp_path / "bad.zip").write_text("nope")
    assert cli.main(["logs", "--read", str(tmp_path / "bad.zip")]) == 2
