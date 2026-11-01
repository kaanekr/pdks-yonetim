"""Compile real Storage.cpp/Attendance.cpp with an SD-enabled soft-SPI fake.

Builds the native test/test_named_sd.cpp with the production flags
PDKS_ENABLE_SD=1, PDKS_SD_SOFTSPI=1, SPI_DRIVER_SELECT=3, BUZZER_GPIO=1,
UART_LOGGING=0 plus fault injection. pdks::softsd is provided by the test
(SoftSdCard.cpp is NOT linked). No device, no serial, no network.
"""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]

FLAGS = [
    '-DPDKS_ENABLE_SD=1',
    '-DPDKS_SD_SOFTSPI=1',
    '-DSPI_DRIVER_SELECT=3',
    '-DPDKS_ENABLE_BUZZER=1',
    '-DPDKS_BUZZER_GPIO=1',
    '-DPDKS_UART_LOGGING=0',
    '-DPDKS_REWRITE_FAULTS',
]


def main():
    compiler = (os.environ.get('CXX') or shutil.which('g++')
                or str(ROOT/'.platformio/packages/toolchain-gccmingw32/bin/g++.exe'))
    library = ROOT/'.pio/libdeps/nodemcuv2_sd_soft_tx/ArduinoJson/src'
    folder = ROOT/'.pio/named-sd-tests'
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder/'named_sd_tests.exe'
    env = os.environ.copy()
    env['PATH'] = str(Path(compiler).resolve().parent)+os.pathsep+env.get('PATH', '')
    command = [compiler, '-std=c++11', '-Wall', '-Wextra', '-Werror', '-pedantic',
               *FLAGS,
               '-I', str(ROOT/'test/storage_mocks'), '-I', str(ROOT/'include'), '-I', str(library),
               str(ROOT/'test/test_named_sd.cpp'), str(ROOT/'src/Attendance.cpp'), '-o', str(exe)]
    print(subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)
    subprocess.run([str(exe)], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
