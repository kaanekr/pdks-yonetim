"""Exercise the actual storage codec on the host, without Arduino/flash mocks.

Only the pure codec helper block is extracted; device I/O is NOT tested here.
"""
import binascii
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
        raise SystemExit("C++ derleyicisi bulunamadi; CXX ayarlayin.")
    source = (ROOT / "src/Storage.cpp").read_text(encoding="utf-8")
    helpers = source[source.index("uint32_t crc32("):source.index("bool writeAndVerify(")]
    body = "1,1,1792756800,DEADBEEF,I,0"
    expected = f"{body},{binascii.crc32(body.encode()):08X}"
    harness = r'''
#include "Attendance.h"
#include "Config.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <vector>
using pdks::Attendance;
using pdks::Event;
constexpr size_t JOURNAL_MAX_LINE = cfg::JOURNAL_MAX_LINE;
'''
    harness += helpers
    harness += r'''
static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; \
  printf("FAIL line %d: %s\n", __LINE__, #x); } } while (0)
static std::string checked(const std::string& body) {
  char crc[10];
  snprintf(crc, sizeof(crc), ",%08lX", (unsigned long)crc32(body.data(), body.size()));
  return body + crc;
}
static bool parse(const std::string& line, Event& event) {
  std::vector<char> buffer(line.begin(), line.end());
  buffer.push_back('\0');
  uint32_t seq = 0, epoch = 0;
  return parseLine(buffer.data(), line.size(), event, seq, epoch);
}
int main() {
  CHECK(crc32("123456789", 9) == 0xCBF43926UL);
  Event event;
  event.sequence = 1; event.epoch = 1792756800UL;
  strcpy(event.uid, "DEADBEEF"); event.type = 'I'; event.seconds = 0;
  char line[128];
  int length = buildLine(line, sizeof(line), event);
  CHECK(length > 0);
'''
    harness += f'  CHECK(strcmp(line, "{expected}") == 0);\n'
    harness += r'''
  Event decoded;
  CHECK(parse(line, decoded));
  CHECK(decoded.sequence == 1 && decoded.epoch == event.epoch);
  CHECK(strcmp(decoded.uid, "DEADBEEF") == 0 && decoded.type == 'I');
  CHECK(decoded.seconds == 0);
  CHECK(parse(checked("1,2,1792785600,DEADBEEF,O,28800"), decoded));
  CHECK(decoded.type == 'O' && decoded.seconds == 28800);
  CHECK(parse(checked("1,1,1792756800,0123456789ABCD,I,0"), decoded));
  CHECK(parse(checked("1,1,1792756800,0123456789ABCDEF0123,I,0"), decoded));
  const char* bad[] = {
    "1,1,1792756800,DEADBEEF,I,0,EXTRA",
    "1,1,1792756800,DEADBEEF,I,0,",
    "1,1,1792756800,DEADBEEF,I",
    "2,1,1792756800,DEADBEEF,I,0",
    "1,0,1792756800,DEADBEEF,I,0",
    "1,01,1792756800,DEADBEEF,I,0",
    "1,-1,1792756800,DEADBEEF,I,0",
    "1,+1,1792756800,DEADBEEF,I,0",
    "1,4294967296,1792756800,DEADBEEF,I,0",
    "1,1,0,DEADBEEF,I,0",
    "1,1,4102444800,DEADBEEF,I,0",
    "1,1,1792756800,deadbeef,I,0",
    "1,1,1792756800,NOTHEX00,I,0",
    "1,1,1792756800,DEADBEEF,X,0",
    "1,1,1792756800,DEADBEEF,II,0",
    "1,1,1792756800,DEADBEEF,I,-1",
    "1,1,1792756800,DEADBEEF,I,00",
    "1,1,1792756800,DEADBEEF,I,4294967296"
  };
  for (const char* body : bad) CHECK(!parse(checked(body), decoded));
  std::string valid(line);
  std::string corrupt = valid;
  corrupt[2] = '2';
  CHECK(!parse(corrupt, decoded));
  CHECK(!parse(valid + "\n", decoded));
  CHECK(!parse(valid + std::string(1, '\0') + "hidden", decoded));
  CHECK(!parse(std::string(200, 'A'), decoded));
  CHECK(!parse("", decoded));
  printf("journal codec: %d checks, %d failure(s)\n", checks, failures);
  return failures ? 1 : 0;
}
'''
    folder = ROOT / ".pio/journal-tests"
    folder.mkdir(parents=True, exist_ok=True)
    cpp = folder / "journal_tests.cpp"
    cpp.write_text(harness, encoding="utf-8")
    executable = folder / ("journal_tests.exe" if os.name == "nt" else "journal_tests")
    env = os.environ.copy()
    env["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + env.get("PATH", "")
    command = [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
               "-I", str(ROOT / "include"), str(cpp), str(ROOT / "src/Attendance.cpp"),
               "-o", str(executable)]
    print(subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)
    subprocess.run([str(executable)], cwd=ROOT, env=env, check=True)


if __name__ == "__main__":
    main()
