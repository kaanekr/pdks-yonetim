"""Test the real scan adapter + release latch with a HALT-aware RFID mock."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    compiler = os.environ.get("CXX") or shutil.which("g++")
    local = ROOT / ".platformio/packages/toolchain-gccmingw32/bin/g++.exe"
    if not compiler and local.exists():
        compiler = str(local)
    if not compiler:
        raise SystemExit("Host C++ compiler required.")
    source = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    read = source[source.index("static pdks::CardPresence readCardUid("):source.index("static void handleCard(")]
    scan = source[source.index("static void scanRfid("):source.index("// ------------------------------------------------------------------- web")]
    assert "PICC_WakeupA" in read and "PICC_IsNewCardPresent" not in read
    folder = ROOT / ".pio/card-gate-tests"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "rfid_scan.inc").write_text(read + scan, encoding="utf-8")
    exe = folder / ("card_gate_tests.exe" if os.name == "nt" else "card_gate_tests")
    environment = os.environ.copy()
    environment["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + environment.get("PATH", "")
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                    "-I", str(ROOT / "include"), "-I", str(folder),
                    str(ROOT / "test/test_card_gate.cpp"), str(ROOT / "src/Attendance.cpp"),
                    "-o", str(exe)], check=True, cwd=ROOT, env=environment)
    subprocess.run([str(exe)], check=True, cwd=ROOT, env=environment)


if __name__ == "__main__":
    main()
