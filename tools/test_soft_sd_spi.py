#!/usr/bin/env python3
"""Build and run the host-side test for the software-SPI SD driver.

Only test/test_soft_sd_spi.cpp is compiled; the driver implementation lives in
include/SoftSdSpiDriver.h, so src/SoftSdCard.cpp (which needs SdFat's file
system) stays out of this build.

$CXX wins when set, otherwise the PlatformIO MinGW g++ is used, otherwise g++
from PATH. The compiler's directory is prepended to PATH for both the compile
and the run step: MinGW's g++.exe needs its sibling DLLs, and a bare
subprocess inherits a PATH that may not contain them.
"""

import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / ".pio" / "soft-sd-tests"
EXE = OUT_DIR / "test_soft_sd_spi.exe"


def find_compiler():
    local = ROOT / ".platformio" / "packages" / "toolchain-gccmingw32" / "bin" / "g++.exe"
    cxx = os.environ.get("CXX")
    if cxx and pathlib.Path(cxx).is_file():
        return pathlib.Path(cxx)
    if local.is_file():
        return local
    found = shutil.which("g++") or shutil.which("c++")
    return pathlib.Path(found) if found else None


def main():
    compiler = find_compiler()
    if compiler is None:
        print("ERROR: no host C++ compiler found ($CXX, MinGW toolchain or g++).")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # MinGW g++.exe (2015 build) fails with exit 1 and no stderr when its DLLs
    # are not on PATH; make the toolchain directory visible to child processes.
    environment = os.environ.copy()
    environment["PATH"] = (
        str(compiler.resolve().parent) + os.pathsep + environment.get("PATH", "")
    )

    command = [
        str(compiler),
        "-std=c++11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-pedantic",
        "-DPDKS_ENABLE_SD=1",
        "-DPDKS_SD_SOFTSPI=1",
        "-DSPI_DRIVER_SELECT=3",
        "-I", str(ROOT / "test" / "mocks"),
        "-I", str(ROOT / "include"),
        "-I", str(ROOT / "test"),
        str(ROOT / "test" / "test_soft_sd_spi.cpp"),
        "-o", str(EXE),
    ]

    profiles = {
        "silent": ["-DPDKS_ENABLE_BUZZER=0"],
        "tx": ["-DPDKS_ENABLE_BUZZER=1", "-DPDKS_BUZZER_GPIO=1",
               "-DPDKS_UART_LOGGING=0", "-DPDKS_BOOT_BEEP_TEST=1"],
    }
    for name, flags in profiles.items():
        exe = OUT_DIR / f"test_soft_sd_spi_{name}.exe"
        build = command[:-2] + flags + ["-o", str(exe)]
        print(f"Compiling [{name}]:", " ".join(build), flush=True)
        compiled = subprocess.run(build, cwd=str(ROOT), env=environment)
        if compiled.returncode != 0:
            return compiled.returncode
        print("Running:", exe, flush=True)
        run = subprocess.run([str(exe)], cwd=str(ROOT), env=environment)
        if run.returncode != 0:
            return run.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
