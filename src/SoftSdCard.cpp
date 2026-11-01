#include "SoftSdCard.h"

#include "Config.h"
#include "Console.h"

#if PDKS_SD_SOFTSPI

#include <Arduino.h>
#include <SPI.h>
#include <SdFat.h>
#include <string.h>

#include "SoftSdSpiDriver.h"

#if SPI_DRIVER_SELECT != 3
#error "PDKS_SD_SOFTSPI requires -DSPI_DRIVER_SELECT=3 (external SdSpiBaseClass driver)"
#endif

namespace pdks {
namespace softsd {
namespace {

constexpr char EVENTS_PATH[] = "/events.log";
constexpr uint32_t SD_SOFT_SCK_HZ = 250000UL; // ~4 us bit time, well below SD limits

SdFat32 fs;
Driver driver;
bool g_started = false;

// Every exit path must return the shared SCK/MOSI pins to the hardware SPI
// peripheral, otherwise the RC522 cannot be read after a failed SD operation.
struct RestoreBus {
  ~RestoreBus() { driver.deactivate(); }
};

void logMountFailure() {
  console::printf("[SD] soft-spi mount failed: err=0x%02X data=0x%02X\r\n",
                (unsigned)fs.sdErrorCode(), (unsigned)fs.sdErrorData());
}

} // namespace

bool begin() {
  if (g_started) return true;

  // The RC522 must stay deselected for the whole mount attempt.
  pinMode(cfg::PIN_RFID_CS, OUTPUT);
  digitalWrite(cfg::PIN_RFID_CS, HIGH);

  RestoreBus restore; // restores hardware SPI even when mount fails
  SdSpiConfig spiConfig(cfg::PIN_SD_CS, SHARED_SPI, SD_SOFT_SCK_HZ, &driver);
  if (!fs.begin(spiConfig)) {
    logMountFailure();
    return false;
  }
  g_started = true;
  console::printf("[SD] soft-spi mounted, FAT%u\r\n", (unsigned)fs.fatType());
  return true;
}

bool appendLine(const char* data, size_t length) {
  if (!g_started || !data) return false;
  // One raw line plus '\n' must fit the journal line bound. This also keeps
  // the read-back stack buffer bounded and avoids length + 1 overflow.
  if (length == 0 || length >= cfg::JOURNAL_MAX_LINE) return false;

  const uint32_t need = (uint32_t)length + 1;
  RestoreBus restore; // restores hardware SPI on every early return

  File32 f = fs.open(EVENTS_PATH, O_WRONLY | O_CREAT | O_APPEND);
  if (!f) {
    console::println("[SD] soft-spi: open events.log for append failed");
    return false;
  }
  bool ok = f.write(data, length) == length;
  if (ok) ok = f.write("\n", 1) == 1;
  if (ok) ok = f.sync();
  const bool closed = f.close();
  if (!ok || !closed) {
    console::println("[SD] soft-spi: append/sync/close failed");
    return false;
  }

  // Verify the appended tail through a fresh read handle. No scan/rewrite.
  File32 back = fs.open(EVENTS_PATH, O_RDONLY);
  if (!back) {
    console::println("[SD] soft-spi: reopen events.log for verify failed");
    return false;
  }
  const uint32_t size = back.fileSize();
  bool verified = false;
  if (size >= need && back.seekSet(size - need)) {
    char tail[cfg::JOURNAL_MAX_LINE];
    const int got = back.read(tail, need);
    verified = got >= 0 && (uint32_t)got == need && tail[length] == '\n' &&
               memcmp(tail, data, length) == 0;
  }
  back.close();
  if (!verified) {
    console::println("[SD] soft-spi: readback verify failed");
  }
  return verified;
}

bool resetEvents() {
  if (!g_started) return false;
  RestoreBus restore;
  if (fs.exists(EVENTS_PATH) && !fs.remove(EVENTS_PATH)) return false;
  return !fs.exists(EVENTS_PATH);
}

} // namespace softsd
} // namespace pdks

#else // !PDKS_SD_SOFTSPI

namespace pdks {
namespace softsd {

// Base variants keep the same symbols without pulling SdFat/SPI into the build.
bool begin() { return false; }

bool appendLine(const char*, size_t) { return false; }
bool resetEvents() { return false; }

} // namespace softsd
} // namespace pdks

#endif // PDKS_SD_SOFTSPI
