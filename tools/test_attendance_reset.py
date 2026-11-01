"""Test the actual maintenance IO adapter and reset engine; never access a device."""
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
    production = (ROOT / "src/ResetAttendance.cpp").read_text(encoding="utf-8")
    adapter = "struct ResetIo {" + production.split("struct ResetIo {", 1)[1].split("static void message", 1)[0]
    assert "LittleFS.format(" not in production
    assert 'reset::run(io, REQUEST)' in production
    assert "#if defined(PDKS_ATTENDANCE_RESET_ID)" in production
    template = (ROOT / "test/test_attendance_reset.cpp").read_text(encoding="utf-8")
    folder = ROOT / ".pio/reset-tests"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "reset_tests.cpp"
    source.write_text(template.replace("// RESET_IO", adapter), encoding="utf-8")
    exe = folder / ("reset_tests.exe" if os.name == "nt" else "reset_tests")
    environment = os.environ.copy()
    environment["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + environment.get("PATH", "")
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                    "-I", str(ROOT / "include"), str(source), str(ROOT / "src/Attendance.cpp"),
                    "-o", str(exe)], check=True, cwd=ROOT, env=environment)
    subprocess.run([str(exe)], check=True, cwd=ROOT, env=environment)


if __name__ == "__main__":
    main()
