// Host-side unit checks for pdks::softsd::Driver (include/SoftSdSpiDriver.h).
//
// The real header is compiled as-is against minimal Arduino/SPI/SdFat mocks,
// so the production code is what gets exercised. The test deliberately does
// not link src/SoftSdCard.cpp: SdFat's file system is not mocked here.

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <Arduino.h>
#include <SPI.h>
#include <SdFat.h>

#include "Config.h"
#include "SoftSdSpiDriver.h"

SPIClass SPI; // mock instance used by the driver

namespace {

int g_failures = 0;
int g_checks = 0;

#define CHECK(cond)                                                      \
  do {                                                                   \
    ++g_checks;                                                          \
    if (!(cond)) {                                                       \
      ++g_failures;                                                      \
      printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);             \
    }                                                                    \
  } while (0)

mock::State& st() { return mock::state(); }

int findEventFrom(size_t from, mock::EventKind kind, int pin, int value) {
  for (size_t i = from; i < st().events.size(); ++i) {
    const mock::Event& e = st().events[i];
    if (e.kind == kind && e.pin == pin && e.value == value) return (int)i;
  }
  return -1;
}

bool hasPinEventFrom(size_t from, int pin) {
  for (size_t i = from; i < st().events.size(); ++i) {
    if (st().events[i].pin == pin) return true;
  }
  return false;
}

void checkGpio16Safety() {
  CHECK(st().pinModeValue[cfg::PIN_SD_SOFT_MISO] == INPUT);
  CHECK(st().output16Count == 0);
  CHECK(st().digitalWrite16Count == 0);
  for (size_t i = 0; i < st().events.size(); ++i) {
    const mock::Event& e = st().events[i];
    if (e.pin != (int)cfg::PIN_SD_SOFT_MISO) continue;
    CHECK(e.kind == mock::EV_PIN_MODE);
    CHECK(e.value == INPUT);
  }
}

void feedByte(uint8_t value) {
  for (int bit = 7; bit >= 0; --bit) {
    st().miso.push_back((uint8_t)((value >> bit) & 1u));
  }
}

uint8_t sampledByte(size_t offset) {
  uint8_t value = 0;
  for (size_t i = 0; i < 8; ++i) {
    value = (uint8_t)((value << 1) | st().sent[offset + i]);
  }
  return value;
}

void checkDelays(unsigned int expected) {
  CHECK(st().delays.size() == 16);
  for (size_t i = 0; i < st().delays.size(); ++i) {
    CHECK(st().delays[i] == expected);
  }
}

void testBeginKeepsMisoInputOnly() {
  mock::reset();
  pdks::softsd::Driver driver;
  SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 400000UL, &driver);
  driver.begin(config);

  CHECK(st().pinModeValue[cfg::PIN_SD_SOFT_MISO] == INPUT);
  CHECK(st().output16Count == 0);
  CHECK(st().digitalWrite16Count == 0);
  CHECK(st().spiBeginCount == 0); // begin must not claim the hardware bus
  CHECK(st().spiEndCount == 0);
  checkGpio16Safety();
}

void testActivateTakesBusSafely() {
  mock::reset();
  pdks::softsd::Driver driver;
  SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 250000UL, &driver);
  driver.begin(config);
  driver.setSckSpeed(250000UL);

  const size_t before = st().events.size();
  driver.activate();

  CHECK(st().pinLevel[cfg::PIN_RFID_CS] == HIGH);
  CHECK(st().spiEndCount == 1);
  CHECK(st().pinModeValue[cfg::SPI_SCK_GPIO] == OUTPUT);
  CHECK(st().pinLevel[cfg::SPI_SCK_GPIO] == LOW);
  CHECK(st().pinModeValue[cfg::SPI_MOSI_GPIO] == OUTPUT);
  CHECK(st().pinLevel[cfg::SPI_MOSI_GPIO] == HIGH);
  CHECK(st().pinModeValue[cfg::PIN_SD_SOFT_MISO] == INPUT);

  const int csHigh =
      findEventFrom(before, mock::EV_DIGITAL_WRITE, cfg::PIN_RFID_CS, HIGH);
  const int spiEnd = findEventFrom(before, mock::EV_SPI_END, -1, 0);
  const int sckLatch =
      findEventFrom(before, mock::EV_DIGITAL_WRITE, cfg::SPI_SCK_GPIO, LOW);
  const int sckOut =
      findEventFrom(before, mock::EV_PIN_MODE, cfg::SPI_SCK_GPIO, OUTPUT);
  const int mosiLatch =
      findEventFrom(before, mock::EV_DIGITAL_WRITE, cfg::SPI_MOSI_GPIO, HIGH);
  const int mosiOut =
      findEventFrom(before, mock::EV_PIN_MODE, cfg::SPI_MOSI_GPIO, OUTPUT);

  CHECK(csHigh >= 0);
  CHECK(spiEnd > csHigh);        // RC522 deselected before bus changes owners
  CHECK(sckLatch > spiEnd);      // idle level latched after HSPI released
  CHECK(sckOut > sckLatch);      // output driver enabled after the latch
  CHECK(mosiLatch > spiEnd);
  CHECK(mosiOut > mosiLatch);
  CHECK(st().pinLevel[cfg::SPI_SCK_GPIO] == LOW);
  CHECK(!hasPinEventFrom(before, cfg::PIN_SD_CS)); // SdFat owns SD CS
  checkGpio16Safety();
}

void testReceiveMode0MsbFirst() {
  mock::reset();
  pdks::softsd::Driver driver;
  SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 250000UL, &driver);
  driver.begin(config);
  driver.setSckSpeed(250000UL);
  driver.activate();

  st().sent.clear();
  feedByte(0xA5);
  const uint8_t got = driver.receive();

  CHECK(got == 0xA5);
  CHECK(st().sent.size() == 8);
  CHECK(st().misoPos == 8);
  CHECK(!st().sampledWrongEdge); // MISO sampled only while SCK is HIGH
  CHECK(st().pinLevel[cfg::SPI_SCK_GPIO] == LOW);
  for (size_t i = 0; i < st().sent.size(); ++i) {
    CHECK(st().sent[i] == 1); // receive clocks 0xFF out on MOSI
  }
  CHECK(sampledByte(0) == 0xFF);
  checkGpio16Safety();
}

void testSendMode0MsbFirst() {
  mock::reset();
  pdks::softsd::Driver driver;
  SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 250000UL, &driver);
  driver.begin(config);
  driver.setSckSpeed(250000UL);
  driver.activate();

  st().sent.clear();
  driver.send(0x3C);
  CHECK(st().sent.size() == 8);
  CHECK(sampledByte(0) == 0x3C);
  CHECK(st().pinLevel[cfg::SPI_SCK_GPIO] == LOW);
  CHECK(st().pinLevel[cfg::SPI_MOSI_GPIO] == LOW); // last bit of 0x3C is 0
  checkGpio16Safety();
}

void testMultiByteTransfersYieldLow() {
  mock::reset();
  pdks::softsd::Driver driver;
  SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 250000UL, &driver);
  driver.begin(config);
  driver.setSckSpeed(250000UL);
  driver.activate();

  uint8_t pattern[64];
  for (size_t i = 0; i < sizeof(pattern); ++i) {
    pattern[i] = (uint8_t)(i * 7u + 3u);
    feedByte(pattern[i]);
  }
  uint8_t in[64];
  memset(in, 0, sizeof(in));
  CHECK(driver.receive(in, sizeof(in)) == 0);
  CHECK(memcmp(in, pattern, sizeof(pattern)) == 0);
  CHECK(!st().sampledWrongEdge);
  CHECK(st().yields >= 1);
  CHECK(st().lastYieldSckLow);
  CHECK(st().pinLevel[cfg::SPI_SCK_GPIO] == LOW);

  st().sent.clear();
  driver.send(pattern, sizeof(pattern));
  CHECK(st().sent.size() == sizeof(pattern) * 8);
  for (size_t i = 0; i < sizeof(pattern); ++i) {
    CHECK(sampledByte(i * 8) == pattern[i]);
  }
  CHECK(st().lastYieldSckLow);
  checkGpio16Safety();
}

void testSckSpeedClamp() {
  mock::reset();
  pdks::softsd::Driver driver;
  SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 250000UL, &driver);
  driver.begin(config);
  driver.activate();

  driver.setSckSpeed(250000UL);
  st().delays.clear();
  driver.send(0x00);
  checkDelays(2); // fastest allowed: 2 us half period

  driver.setSckSpeed(1000000UL);
  st().delays.clear();
  driver.send(0x00);
  checkDelays(2); // clamped down to 250 kHz

  driver.setSckSpeed(0xFFFFFFFFUL);
  st().delays.clear();
  driver.send(0x00);
  checkDelays(2); // overflow-safe division

  driver.setSckSpeed(0);
  st().delays.clear();
  driver.send(0x00);
  checkDelays(2); // zero frequency guard

  driver.setSckSpeed(125000UL);
  st().delays.clear();
  driver.send(0x00);
  checkDelays(4);

  driver.setSckSpeed(200000UL);
  st().delays.clear();
  driver.send(0x00);
  checkDelays(3); // ceil(500000 / 200000) = 3 us, not 2

  driver.setSckSpeed(1);
  st().delays.clear();
  driver.send(0x00);
  checkDelays(1000); // upper sanity clamp
  checkGpio16Safety();
}

void testSingleByteBusyPollYieldsLow() {
  mock::reset();
  pdks::softsd::Driver driver;
  SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 250000UL, &driver);
  driver.begin(config);
  driver.activate();
  for (size_t i = 0; i < 64; ++i) CHECK(driver.receive() == 0xFF);
  CHECK(st().yields == 2);
  CHECK(st().lastYieldSckLow);
  CHECK(!st().sampledWrongEdge);
  checkGpio16Safety();
}

void testBeginSpeedSelection() {
  mock::reset();
  {
    pdks::softsd::Driver driver;
    SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 100000UL, &driver);
    driver.begin(config);
    driver.activate();
    st().delays.clear();
    driver.send(0x00);
    checkDelays(5); // fallback config.maxSck: 500000 / 100000
  }
  {
    mock::reset();
    pdks::softsd::Driver driver;
    driver.setSckSpeed(50000UL); // SdFat's init rate must win over begin(config)
    SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 250000UL, &driver);
    driver.begin(config);
    driver.activate();
    st().delays.clear();
    driver.send(0x00);
    checkDelays(10);
  }
  {
    mock::reset();
    pdks::softsd::Driver driver;
    SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 0, &driver);
    driver.begin(config);
    driver.activate();
    st().delays.clear();
    driver.send(0x00);
    checkDelays(2); // zero config speed guard
  }
  checkGpio16Safety();
}

void testDeactivateRestoresHardwareSpi() {
  mock::reset();
  pdks::softsd::Driver driver;
  SdSpiConfig config(cfg::PIN_SD_CS, SHARED_SPI, 250000UL, &driver);
  driver.begin(config);
  driver.setSckSpeed(250000UL);
  driver.activate();
  driver.send(0xFF);

  const size_t before = st().events.size();
  driver.deactivate();

  CHECK(st().pinLevel[cfg::PIN_SD_CS] == HIGH);
  CHECK(st().pinModeValue[cfg::SPI_SCK_GPIO] == SPECIAL);
  CHECK(st().pinModeValue[cfg::SPI_MOSI_GPIO] == SPECIAL);
  CHECK(st().pinModeValue[cfg::SPI_MISO_GPIO] == SPECIAL);
  CHECK(st().pinModeValue[cfg::PIN_SD_SOFT_MISO] == INPUT);
  CHECK(st().spiBeginCount == 1);
  CHECK(st().spiHwCsCount >= 1);
  CHECK(st().spiLastHwCs == false);
  CHECK(findEventFrom(before, mock::EV_DIGITAL_WRITE, cfg::PIN_SD_CS, HIGH) >= 0);
  CHECK(findEventFrom(before, mock::EV_SPI_BEGIN, -1, 0) >= 0);
  CHECK(findEventFrom(before, mock::EV_SPI_HWCS, -1, 0) >= 0);
  checkGpio16Safety();

  // Calling deactivate again (failed begin/append wrapper path) is safe.
  driver.deactivate();
  CHECK(st().pinModeValue[cfg::SPI_SCK_GPIO] == SPECIAL);
  CHECK(st().pinModeValue[cfg::SPI_MOSI_GPIO] == SPECIAL);
  CHECK(st().pinModeValue[cfg::PIN_SD_SOFT_MISO] == INPUT);
  CHECK(st().spiBeginCount == 2);
  checkGpio16Safety();
}

} // namespace

int main() {
  testBeginKeepsMisoInputOnly();
  testActivateTakesBusSafely();
  testReceiveMode0MsbFirst();
  testSendMode0MsbFirst();
  testMultiByteTransfersYieldLow();
  testSckSpeedClamp();
  testSingleByteBusyPollYieldsLow();
  testBeginSpeedSelection();
  testDeactivateRestoresHardwareSpi();

  if (g_failures != 0) {
    printf("%d checks, %d failure(s)\n", g_checks, g_failures);
    return 1;
  }
  printf("%d checks, 0 failure(s)\n", g_checks);
  printf("all soft-SPI driver checks passed\n");
  return 0;
}
