#!/usr/bin/env python3
"""Build and run the host-side tests for include/Console.h.

test/test_console.cpp is compiled twice against the real include/Config.h and
the isolated test/console_mocks/Arduino.h:

  default  UART logging on, buzzer on D0/GPIO16 (no extra defines)
  TX       soft-SPI SD, GPIO1 buzzer, UART logging off (full target flags)

Two negative builds prove Config.h rejects unsafe combinations:
  * GPIO1 buzzer with UART logging on
  * soft-SPI SD plus a D0/GPIO16 buzzer

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
OUT_DIR = ROOT / ".pio" / "console-tests"
SOURCE = ROOT / "test" / "test_console.cpp"
GUARD_SOURCE = OUT_DIR / "config_guard.cpp"

WARN_FLAGS = ["-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic"]

TX_FLAGS = [
    "-DPDKS_ENABLE_SD=1",
    "-DPDKS_SD_SOFTSPI=1",
    "-DPDKS_ENABLE_BUZZER=1",
    "-DPDKS_BUZZER_GPIO=1",
    "-DPDKS_UART_LOGGING=0",
    "-DPDKS_BOOT_BEEP_TEST=1",
    "-DSPI_DRIVER_SELECT=3",
]


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

    include_flags = [
        "-I", str(ROOT / "test" / "console_mocks"),
        "-I", str(ROOT / "include"),
        "-I", str(ROOT / "test"),
    ]

    profiles = [
        ("default-uart", [], OUT_DIR / "test_console_default.exe"),
        ("tx-no-uart", TX_FLAGS, OUT_DIR / "test_console_tx.exe"),
    ]

    for name, defines, exe in profiles:
        command = (
            [str(compiler)]
            + WARN_FLAGS
            + defines
            + include_flags
            + [str(SOURCE), "-o", str(exe)]
        )
        print("Compiling [%s]:" % name, " ".join(command))
        sys.stdout.flush()
        compiled = subprocess.run(command, cwd=str(ROOT), env=environment)
        if compiled.returncode != 0:
            print("ERROR: compilation failed for", name, "with exit code",
                  compiled.returncode)
            return compiled.returncode

        print("Running [%s]:" % name, exe)
        sys.stdout.flush()
        run = subprocess.run([str(exe)], cwd=str(ROOT), env=environment)
        if run.returncode != 0:
            print("ERROR: test", name, "failed with exit code", run.returncode)
            return run.returncode

    GUARD_SOURCE.write_text('#include "Config.h"\n', encoding="utf-8")
    guards = [
        (
            "gpio1-uart-on",
            TX_FLAGS[:4] + ["-DPDKS_UART_LOGGING=1"] + TX_FLAGS[5:],
            "TX buzzer requires UART logging disabled",
        ),
        (
            "sd-soft-gpio16-buzzer",
            [
                "-DPDKS_ENABLE_SD=1",
                "-DPDKS_SD_SOFTSPI=1",
                "-DPDKS_ENABLE_BUZZER=1",
                "-DPDKS_BUZZER_GPIO=16",
                "-DSPI_DRIVER_SELECT=3",
            ],
            "D0 cannot be both",
        ),
    ]

    for name, defines, expected in guards:
        command = (
            [str(compiler)]
            + WARN_FLAGS
            + ["-fsyntax-only"]
            + defines
            + ["-I", str(ROOT / "include"), str(GUARD_SOURCE)]
        )
        print("Compiling guard [%s] (must fail):" % name, " ".join(command))
        sys.stdout.flush()
        guard = subprocess.run(
            command,
            cwd=str(ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output = guard.stdout or ""
        if guard.returncode == 0:
            print("ERROR: unsafe config", name, "was accepted")
            return 1
        if expected not in output:
            print("ERROR: guard", name, "failed without the expected message:")
            print(output)
            return 1
        print("Guard [%s] rejected as expected: %s" % (name, expected))

    print("All console checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
