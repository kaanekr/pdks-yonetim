"""Host regression test of the actual setBuzzer() helper with GPIO stubs.

This checks software switching/idle levels, NOT physical sound or voltage.
"""
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
        raise SystemExit("C++ derleyicisi gerekli; CXX ayarlayin.")
    source = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    helper = source[source.index("static void setBuzzer("):source.index("static void beepPlay(")]
    prefix = r'''
#include <stdint.h>
#include <stdio.h>
enum { LOW = 0, HIGH = 1 };
namespace cfg {
const uint8_t PIN_BUZZER = TEST_BUZZER_GPIO;
bool BUZZER_ACTIVE_HIGH = false;
bool ENABLE_BUZZER = true;
uint16_t BUZZER_TONE_HZ = 2000;
}
static bool g_buzzerToneRunning = false;
static int toneCalls = 0, stopCalls = 0, level = -1, frequency = 0, lastPin = -1;
static int wrongPinCalls = 0;
static void tone(uint8_t pin, unsigned int hz) {
  if (pin != cfg::PIN_BUZZER) ++wrongPinCalls;
  ++toneCalls; lastPin = pin; frequency = hz;
}
static void noTone(uint8_t pin) {
  if (pin != cfg::PIN_BUZZER) ++wrongPinCalls;
  ++stopCalls; lastPin = pin; level = LOW; // real ESP8266 noTone() leaves LOW
}
static void digitalWrite(uint8_t pin, int value) {
  if (pin != cfg::PIN_BUZZER) ++wrongPinCalls;
  lastPin = pin; level = value;
}
'''
    tests = r'''
static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; \
  printf("FAIL line %d: %s\n", __LINE__, #x); } } while (0)
int main() {
  setBuzzer(false);
  CHECK(level == HIGH); CHECK(stopCalls == 0); CHECK(lastPin == cfg::PIN_BUZZER);
  setBuzzer(true);
  CHECK(toneCalls == 1); CHECK(frequency == 2000); CHECK(g_buzzerToneRunning);
  setBuzzer(false);
  CHECK(stopCalls == 1); CHECK(level == HIGH); CHECK(!g_buzzerToneRunning);
  setBuzzer(false);
  CHECK(stopCalls == 1); CHECK(level == HIGH);
  cfg::BUZZER_TONE_HZ = 0;
  setBuzzer(true);
  CHECK(level == LOW); CHECK(toneCalls == 1); CHECK(!g_buzzerToneRunning);
  setBuzzer(false);
  CHECK(level == HIGH);
  cfg::BUZZER_ACTIVE_HIGH = true;
  setBuzzer(true); CHECK(level == HIGH);
  setBuzzer(false); CHECK(level == LOW);
  cfg::BUZZER_ACTIVE_HIGH = false;
  cfg::BUZZER_TONE_HZ = 2000;
  setBuzzer(true); CHECK(g_buzzerToneRunning);
  cfg::BUZZER_TONE_HZ = 0;
  setBuzzer(true);
  CHECK(!g_buzzerToneRunning); CHECK(stopCalls == 2); CHECK(level == LOW);
  setBuzzer(false); CHECK(level == HIGH);
  cfg::BUZZER_TONE_HZ = 2000;
  setBuzzer(true, 1568); CHECK(frequency == 1568); CHECK(g_buzzerToneRunning);
  setBuzzer(true, 2349); CHECK(frequency == 2349); CHECK(g_buzzerToneRunning);
  setBuzzer(false); CHECK(level == HIGH); CHECK(!g_buzzerToneRunning);
  cfg::ENABLE_BUZZER = false;
  const int tonesBefore = toneCalls, stopsBefore = stopCalls;
  level = -1; lastPin = -1;
  setBuzzer(true); setBuzzer(false);
  CHECK(toneCalls == tonesBefore); CHECK(stopCalls == stopsBefore);
  CHECK(level == -1); CHECK(lastPin == -1); // SD owns D0: no output writes
  CHECK(wrongPinCalls == 0); // TX profile must never touch SD MISO GPIO16
  printf("buzzer GPIO%d: %d checks, %d failure(s)\n", cfg::PIN_BUZZER, checks, failures);
  return failures ? 1 : 0;
}
'''
    folder = ROOT / ".pio/buzzer-tests"
    folder.mkdir(parents=True, exist_ok=True)
    cpp = folder / "buzzer_tests.cpp"
    cpp.write_text(prefix + helper + tests, encoding="utf-8")
    environment = os.environ.copy()
    environment["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + environment.get("PATH", "")
    for gpio in (16, 1):
        exe = folder / (f"buzzer_gpio{gpio}" + (".exe" if os.name == "nt" else ""))
        subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                        f"-DTEST_BUZZER_GPIO={gpio}", str(cpp), "-o", str(exe)],
                       check=True, cwd=ROOT, env=environment)
        subprocess.run([str(exe)], check=True, cwd=ROOT, env=environment)


if __name__ == "__main__":
    main()
