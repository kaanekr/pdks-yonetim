"""Compile the real manual-attendance core/storage with the pinned JSON library.

Output stays inside .pio/manual-attendance-tests so it never collides with the
other native test runners.
"""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    compiler = os.environ.get('CXX') or shutil.which('g++') or str(
        ROOT/'.platformio/packages/toolchain-gccmingw32/bin/g++.exe')
    library = ROOT/'.pio/libdeps/nodemcuv2_sd_soft_tx/ArduinoJson/src'
    folder = ROOT/'.pio/manual-attendance-tests'
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder/'manual_attendance_tests.exe'
    env = os.environ.copy()
    env['PATH'] = str(Path(compiler).resolve().parent) + os.pathsep + env.get('PATH', '')
    subprocess.run([compiler, '-std=c++11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                    '-I', str(ROOT/'test/storage_mocks'), '-I', str(ROOT/'include'),
                    '-I', str(library), str(ROOT/'test/test_manual_attendance.cpp'),
                    str(ROOT/'src/Attendance.cpp'), '-o', str(exe)],
                   cwd=ROOT, env=env, check=True)
    subprocess.run([str(exe)], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
