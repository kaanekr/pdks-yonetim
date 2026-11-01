"""Exercise the production OLED renderer with a bounds-checking GFX canvas."""
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
        raise SystemExit("C++ compiler required (CXX or g++).")
    environment = os.environ.copy()
    environment["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + environment.get("PATH", "")
    folder = ROOT / ".pio/display-tests"
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / ("display_tests.exe" if os.name == "nt" else "display_tests")
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                    "-I", str(ROOT / "include"), str(ROOT / "test/test_display.cpp"),
                    "-o", str(exe)], check=True, cwd=ROOT, env=environment)
    subprocess.run([str(exe)], check=True, cwd=ROOT, env=environment)


if __name__ == "__main__":
    main()
