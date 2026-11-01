"""Compile named-event core contracts without hardware."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    compiler = os.environ.get('CXX') or shutil.which('g++') or str(ROOT/'.platformio/packages/toolchain-gccmingw32/bin/g++.exe')
    out = ROOT/'.pio/named-core-tests'
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PATH=str(Path(compiler).resolve().parent)+os.pathsep+os.environ.get('PATH', ''))
    exe = out/'named_core.exe'
    subprocess.run([compiler, '-std=c++11', '-Wall', '-Wextra', '-Werror', '-pedantic', '-I', str(ROOT/'include'),
                    str(ROOT/'test/test_named_core.cpp'), str(ROOT/'src/Attendance.cpp'), '-o', str(exe)], cwd=ROOT, env=env, check=True)
    print('named core:', end=' ', flush=True)
    subprocess.run([str(exe)], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
