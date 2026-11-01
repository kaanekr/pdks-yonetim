// Appended to the actual main.cpp helpers by tools/test_feedback.py.
static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; \
  printf("FAIL line %d: %s\n", __LINE__, #x); } } while (0)

static void checkPattern(const cfg::BuzzerStep* pattern, uint8_t count, uint32_t start) {
  g_now = start;
  beepPlay(pattern, count);
  if (!cfg::ENABLE_BUZZER) {
    CHECK(!g_beep.running); beepTick(g_now); CHECK(tones.empty()); return;
  }
  CHECK(g_beep.running); CHECK(levels[cfg::PIN_BUZZER] == HIGH);
  for (uint8_t i = 0; i < count; ++i) {
    const size_t before = tones.size();
    beepTick(g_now);
    CHECK(g_beep.index == i + 1);
    if (pattern[i].hz) {
      CHECK(tones.size() == before + 1);
      CHECK(tones.back().hz == pattern[i].hz);
      CHECK(tones.back().pin == cfg::PIN_BUZZER);
    } else {
      CHECK(tones.size() == before); CHECK(levels[cfg::PIN_BUZZER] == HIGH);
      CHECK(!g_buzzerToneRunning);
    }
    const size_t after = tones.size();
    g_now += pattern[i].ms;
    beepTick(g_now - 1); // note does not end one millisecond early
    CHECK(tones.size() == after); CHECK(g_beep.running);
  }
  beepTick(g_now);
  CHECK(!g_beep.running); CHECK(!g_buzzerToneRunning);
  CHECK(levels[cfg::PIN_BUZZER] == HIGH);
}

int main() {
  CHECK(cfg::PIN_LED_SIGNAL == 2); CHECK(!cfg::LED_ACTIVE_HIGH);
  setLed(false); CHECK(levels[2] == HIGH);
  const int idleWrites = writes[2];
  ledTick(1000); CHECK(levels[2] == HIGH); CHECK(writes[2] == idleWrites);
  ledPulse(2000); CHECK(levels[2] == LOW);
  ledTick(2000 + cfg::LED_OK_ON_MS - 1); CHECK(levels[2] == LOW);
  ledTick(2000 + cfg::LED_OK_ON_MS); CHECK(levels[2] == HIGH); CHECK(!g_ledPulse);
  ledPulse(1000); ledTick(999); CHECK(levels[2] == LOW); // stale loop snapshot
  ledTick(1000 + cfg::LED_OK_ON_MS); CHECK(levels[2] == HIGH);
  ledPulse(0xFFFFFF00u);
  ledTick(0xFFFFFF00u + cfg::LED_OK_ON_MS - 1); CHECK(levels[2] == LOW);
  ledTick(0xFFFFFF00u + cfg::LED_OK_ON_MS); CHECK(levels[2] == HIGH);
  const int sdWrites = writes[16];
  checkPattern(cfg::BEEP_ENTRY, 3, 100);
  checkPattern(cfg::BEEP_EXIT, 3, 1000);
  CHECK(sizeof(cfg::BEEP_WARN) / sizeof(cfg::BEEP_WARN[0]) == 1);
  CHECK(cfg::BEEP_WARN[0].ms == 700);
  CHECK(cfg::BEEP_WARN[0].hz == 1100);
  checkPattern(cfg::BEEP_WARN, 1, 2000);
  checkPattern(cfg::BEEP_WARN, 1, 0xFFFFFF00u); // long warning across timer wrap
  checkPattern(cfg::BEEP_ERROR, 5, 3000);
  checkPattern(cfg::BEEP_TEST, 5, 4000);
  checkPattern(cfg::BEEP_ENTRY, 3, 0xFFFFFFE0u); // note timer wraps safely
  CHECK(cfg::BEEP_ENTRY[0].hz < cfg::BEEP_ENTRY[2].hz);
  CHECK(cfg::BEEP_EXIT[0].hz > cfg::BEEP_EXIT[2].hz);
  CHECK(cfg::BEEP_ERROR[0].hz < cfg::BEEP_ENTRY[0].hz);
  g_now = 5000; beepPlay(cfg::BEEP_ENTRY, 3); beepTick(g_now);
  beepPlay(cfg::BEEP_ERROR, 5); // replace playing melody without latching LOW
  if (cfg::ENABLE_BUZZER) CHECK(levels[cfg::PIN_BUZZER] == HIGH);
  beepTick(g_now);
  if (cfg::ENABLE_BUZZER) CHECK(tones.back().hz == cfg::BEEP_ERROR[0].hz);
  beepPlay(nullptr, 8); CHECK(!g_beep.running);
  cfg::BuzzerStep longPattern[9] = {};
  beepPlay(longPattern, 9);
  if (cfg::ENABLE_BUZZER) CHECK(g_beep.steps == 8);
  beepPlay(cfg::BEEP_TEST, 0); CHECK(!g_beep.running);
  if (cfg::ENABLE_BUZZER) CHECK(levels[cfg::PIN_BUZZER] == HIGH);
  CHECK(writes[16] == sdWrites); // never drive the separate SD MISO input
  printf("feedback %s: %d checks, %d failure(s)\n",
         cfg::ENABLE_BUZZER ? "TX" : "silent SD", checks, failures);
  return failures ? 1 : 0;
}
