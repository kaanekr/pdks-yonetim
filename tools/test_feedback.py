"""Compile actual main.cpp feedback helpers with GPIO/time mocks; no device I/O."""
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
    source = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    led_state = source[source.index("static uint32_t g_ledOnAt"):source.index("static uint32_t g_clockProbeAt")]
    beep_state = source[source.index("struct Beep {"):source.index("// --------------------------------------------------------------- outputs")]
    helpers = source[source.index("static void setLed("):source.index("// ----------------------------------------------------------------- clock")]
    card = source[source.index("static void handleCard("):source.index("static void scanRfid(")]
    assert card.index("ledPulse(millis())") > card.index("g_core.commit(event)") > card.index("storage::appendEvent(event)")
    # Card cooldown and the panel's warning test both use the single long note.
    assert "beepPlay(cfg::BEEP_WARN, 1);" in card
    sound_test = source.split("static void handleBuzzerTest()", 1)
    assert len(sound_test) == 2, "Sound test handler missing"
    assert 'sound == "warn") beepPlay(cfg::BEEP_WARN, 1);' in sound_test[1].split("\n}", 1)[0]
    assert "beepPlay(cfg::BEEP_WARN, 3)" not in source
    setup = source.split("void setup() {", 1)[1].split("void loop()", 1)[0]
    assert setup.index("setLed(false)") < setup.index("pinMode(cfg::PIN_LED_SIGNAL, OUTPUT)")
    prefix = r'''
#include "Config.h"
#include <stdint.h>
#include <stdio.h>
#include <vector>
enum { LOW = 0, HIGH = 1 };
static uint32_t g_now = 0;
static int levels[17] = {}, writes[17] = {};
struct ToneCall { uint8_t pin; unsigned int hz; };
static std::vector<ToneCall> tones;
static uint32_t millis() { return g_now; }
static void digitalWrite(uint8_t pin, int value) { levels[pin] = value; ++writes[pin]; }
static void tone(uint8_t pin, unsigned int hz) { tones.push_back(ToneCall{pin, hz}); }
static void noTone(uint8_t pin) { levels[pin] = LOW; }
'''
    folder = ROOT / ".pio/feedback-tests"
    folder.mkdir(parents=True, exist_ok=True)
    cpp = folder / "feedback_tests.cpp"
    cpp.write_text(prefix + led_state + beep_state + helpers +
                   (ROOT / "test/test_feedback.cpp").read_text(encoding="utf-8"), encoding="utf-8")
    environment = os.environ.copy()
    environment["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + environment.get("PATH", "")
    profiles = {
        "tx": ["-DPDKS_ENABLE_SD=1", "-DPDKS_SD_SOFTSPI=1", "-DPDKS_ENABLE_BUZZER=1",
               "-DPDKS_BUZZER_GPIO=1", "-DPDKS_UART_LOGGING=0"],
        "silent-sd": ["-DPDKS_ENABLE_SD=1", "-DPDKS_SD_SOFTSPI=1", "-DPDKS_ENABLE_BUZZER=0"],
    }
    for name, flags in profiles.items():
        exe = folder / (name + (".exe" if os.name == "nt" else ""))
        subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                        *flags, "-I", str(ROOT / "include"), str(cpp), "-o", str(exe)],
                       check=True, cwd=ROOT, env=environment)
        subprocess.run([str(exe)], check=True, cwd=ROOT, env=environment)
    print("Feedback integration: success-only LED, inactive startup level OK")


if __name__ == "__main__":
    main()
