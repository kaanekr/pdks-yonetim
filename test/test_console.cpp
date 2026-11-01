// Host-side checks for include/Console.h. tools/test_console.py compiles this
// file twice against the real include/Config.h and test/console_mocks/Arduino.h:
//   default: UART logging on, buzzer on D0/GPIO16
//   TX:      soft-SPI SD on D0, TX/GPIO1 buzzer, UART logging off

#include <cstdio>

#include <Arduino.h>

#include "Config.h"
#include "Console.h"

static int g_checks = 0;
static int g_failures = 0;

// Never compiled out (unlike assert): the counters must survive -DNDEBUG.
#define CHECK(condition)                                                 \
  do {                                                                   \
    ++g_checks;                                                          \
    if (!(condition)) {                                                  \
      ++g_failures;                                                      \
      std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition);   \
    }                                                                    \
  } while (0)

static const char* profileName() {
#if PDKS_UART_LOGGING
  return "default-uart";
#else
  return "tx-no-uart";
#endif
}

#if !PDKS_UART_LOGGING
static int lastEventIndex(console_mock::EventKind kind, int pin) {
  const std::vector<console_mock::Event>& events = console_mock::state().events;
  for (int i = (int)events.size() - 1; i >= 0; --i) {
    if (events[(size_t)i].kind == kind && events[(size_t)i].pin == pin) {
      return i;
    }
  }
  return -1;
}
#endif

int main() {
#if PDKS_UART_LOGGING
  static_assert(PDKS_UART_LOGGING == 1, "default profile keeps UART logging on");
  static_assert(cfg::UART_LOGGING, "cfg::UART_LOGGING mirrors the macro");
  static_assert(cfg::PIN_BUZZER == 16, "default buzzer pin is D0/GPIO16");
  static_assert(!cfg::SD_SOFTSPI, "default profile has no soft-SPI SD");
  static_assert(cfg::ENABLE_BUZZER, "default profile keeps the buzzer enabled");
  static_assert(!cfg::BOOT_BEEP_TEST, "boot beep is off by default");

  console_mock::reset();
  console_mock::state().availableValue = 3;
  console_mock::state().readValue = 'A';

  pdks::console::begin(115200);
  CHECK(console_mock::state().serialBegin == 1);
  CHECK(console_mock::state().serialEnd == 0);
  CHECK(console_mock::state().lastBaud == 115200UL);

  CHECK(pdks::console::available() == 3);
  CHECK(console_mock::state().serialAvailable == 1);
  CHECK(pdks::console::read() == 'A');
  CHECK(console_mock::state().serialRead == 1);

  pdks::console::println();
  pdks::console::println(42);
  pdks::console::println("metin");
  pdks::console::printf("deger=%d\n", 7);
  pdks::console::printf("paket %d %u %ld %lu %s %c\n", -7, 7u, -8L, 8UL,
                        "metin", 'x');
  CHECK(console_mock::state().serialPrintln == 3);
  CHECK(console_mock::state().serialPrintf == 2);
  CHECK(console_mock::state().serialWrite == 0);

  pdks::console::end();
  CHECK(console_mock::state().serialEnd == 1);
  CHECK(console_mock::state().serialBegin == 1);
#else
  static_assert(PDKS_UART_LOGGING == 0, "TX profile disables UART logging");
  static_assert(!cfg::UART_LOGGING, "cfg::UART_LOGGING mirrors the macro");
  static_assert(cfg::PIN_BUZZER == 1, "TX buzzer uses GPIO1");
  static_assert(cfg::ENABLE_SD, "TX profile keeps SD enabled");
  static_assert(cfg::SD_SOFTSPI, "TX profile uses soft-SPI SD");
  static_assert(cfg::ENABLE_BUZZER, "TX profile enables the buzzer");
  static_assert(cfg::BOOT_BEEP_TEST, "TX profile enables the boot beep");
  static_assert(SPI_DRIVER_SELECT == 3, "TX profile selects the soft SPI driver");
  static_assert(cfg::PIN_SD_SOFT_MISO == 16, "SD MISO stays on D0/GPIO16");

  console_mock::reset();
  console_mock::state().availableValue = 3;
  console_mock::state().readValue = 'A';

  pdks::console::begin(115200);
  CHECK(console_mock::state().serialBegin == 0);
  CHECK(console_mock::state().serialEnd == 1);
  CHECK(console_mock::state().lastBaud == 0UL);

  // The caller's next step: prepare GPIO1 now that the UART is released.
  pinMode(cfg::PIN_BUZZER, OUTPUT);
  CHECK(console_mock::state().pinModeValue[1] == OUTPUT);
  const int endIndex = lastEventIndex(console_mock::EV_SERIAL_END, -1);
  const int pinIndex = lastEventIndex(console_mock::EV_PIN_MODE, 1);
  CHECK(endIndex >= 0);
  CHECK(pinIndex > endIndex);

  CHECK(pdks::console::available() == 0);
  CHECK(pdks::console::read() == -1);
  CHECK(console_mock::state().serialAvailable == 0);
  CHECK(console_mock::state().serialRead == 0);

  pdks::console::println();
  pdks::console::println(42);
  pdks::console::println("metin");
  pdks::console::printf("deger=%d\n", 7);
  pdks::console::printf("paket %d %u %ld %lu %s %c\n", -7, 7u, -8L, 8UL,
                        "metin", 'x');
  CHECK(console_mock::state().serialPrintln == 0);
  CHECK(console_mock::state().serialPrintf == 0);
  CHECK(console_mock::state().serialWrite == 0);
  CHECK(console_mock::state().digitalWrites == 0);
  CHECK(console_mock::state().events.size() == 2);

  pdks::console::end();
  CHECK(console_mock::state().serialEnd == 2);
  CHECK(console_mock::state().serialBegin == 0);
#endif

  std::printf("[%s] %d checks, %d failures\n", profileName(), g_checks,
              g_failures);
  return g_failures == 0 ? 0 : 1;
}
