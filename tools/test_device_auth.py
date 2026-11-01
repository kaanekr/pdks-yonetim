"""Compile the real src/DeviceAuth.cpp (FS mocks) as private and public builds."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    compiler = os.environ.get('CXX') or shutil.which('g++') or str(
        ROOT/'.platformio/packages/toolchain-gccmingw32/bin/g++.exe')
    library = ROOT/'.pio/libdeps/nodemcuv2_sd_soft_tx/ArduinoJson/src'
    folder = ROOT/'.pio/device-auth-tests'
    folder.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env['PATH'] = str(Path(compiler).resolve().parent)+os.pathsep+env.get('PATH', '')
    sources = [str(ROOT/'test/test_device_auth.cpp'), str(ROOT/'src/DeviceAuth.cpp')]
    for name, public in (('private', '0'), ('public', '1')):
        exe = folder/('device_auth_%s_tests.exe' % name)
        command = [compiler, '-std=c++11', '-Wall', '-Wextra', '-Werror', '-pedantic',
                   '-DPDKS_PUBLIC_RELEASE='+public,
                   '-I', str(ROOT/'test/storage_mocks'), '-I', str(ROOT/'include'),
                   '-I', str(library)] + sources + ['-o', str(exe)]
        print(subprocess.list2cmdline(command), flush=True)
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        subprocess.run([str(exe)], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
