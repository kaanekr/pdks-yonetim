"""Compile production Wi-Fi codec/manager using pinned ArduinoJson + mocks."""
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
    library = ROOT / ".pio/libdeps/nodemcuv2_sd_soft_tx/ArduinoJson/src"
    if not library.exists():
        raise SystemExit("Install the project's pinned ArduinoJson dependency first.")
    folder = ROOT / ".pio/network-tests"
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / ("network_tests.exe" if os.name == "nt" else "network_tests")
    environment = os.environ.copy()
    environment["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + environment.get("PATH", "")
    subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                    "-I", str(ROOT / "test/network_mocks"), "-I", str(ROOT / "include"),
                    "-I", str(library), str(ROOT / "test/test_network.cpp"),
                    str(ROOT / "src/WifiSettings.cpp"), str(ROOT / "src/Network.cpp"),
                    "-o", str(exe)], check=True, cwd=ROOT, env=environment)
    subprocess.run([str(exe)], check=True, cwd=ROOT, env=environment)


if __name__ == "__main__":
    main()
