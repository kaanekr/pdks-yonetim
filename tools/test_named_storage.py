"""Compile real Storage.cpp with fault-injected FS for named/archived deletes."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    compiler = os.environ.get('CXX') or shutil.which('g++') or str(ROOT/'.platformio/packages/toolchain-gccmingw32/bin/g++.exe')
    library = ROOT/'.pio/libdeps/nodemcuv2_sd_soft_tx/ArduinoJson/src'
    folder = ROOT/'.pio/named-storage-tests'
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder/'named_storage_tests.exe'
    env = os.environ.copy()
    env['PATH'] = str(Path(compiler).resolve().parent)+os.pathsep+env.get('PATH', '')
    command = [compiler, '-std=c++11', '-Wall', '-Wextra', '-Werror', '-pedantic', '-DPDKS_REWRITE_FAULTS',
               '-I', str(ROOT/'test/storage_mocks'), '-I', str(ROOT/'include'), '-I', str(library),
               str(ROOT/'test/test_named_storage.cpp'), str(ROOT/'src/Attendance.cpp'), '-o', str(exe)]
    print(subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)
    subprocess.run([str(exe)], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
