"""Compile and execute the real C++ attendance core without a physical board."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LOCAL_COMPILER = ROOT / ".platformio/packages/toolchain-gccmingw32/bin/g++.exe"


def main():
    compiler = os.environ.get("CXX") or shutil.which("g++")
    if not compiler and LOCAL_COMPILER.exists():
        compiler = str(LOCAL_COMPILER)
    if not compiler:
        raise SystemExit("C++ derleyicisi yok. g++ kurun veya CXX ortam degiskenini ayarlayin.")
    output = ROOT / ".pio/native-tests"
    output.mkdir(parents=True, exist_ok=True)
    executable = output / ("attendance_tests.exe" if os.name == "nt" else "attendance_tests")
    command = [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
               "-I", str(ROOT / "include"), str(ROOT / "src/Attendance.cpp"),
               str(ROOT / "test/test_attendance.cpp"), "-o", str(executable)]
    environment = os.environ.copy()
    environment["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + environment.get("PATH", "")
    print(subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    subprocess.run([str(executable)], cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
