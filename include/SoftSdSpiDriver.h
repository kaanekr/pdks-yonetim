#pragma once

// ---------------------------------------------------------------------------
// Bit-banged SPI driver for the optional microSD mirror (PDKS_SD_SOFTSPI).
//
// Why a custom driver:
//  * The RC522 keeps the hardware HSPI bus (SCK=GPIO14, MOSI=GPIO13,
//    MISO=GPIO12). The SD reader shares SCK/MOSI but uses GPIO16 (D0) as its
//    own MISO input, so the two devices cannot share one hardware SPI config.
//  * SdFat's stock SdSpiSoftDriver has empty activate()/deactivate(), which
//    never frees/restores the HSPI pin mux.
//
// This class is selected with -DSPI_DRIVER_SELECT=3 (external SdSpiBaseClass
// driver) and is handed to SdFat via SdSpiConfig.
//
// Pin rules:
//  * GPIO16 is SD MISO and is only ever configured as INPUT. It is never
//    driven, because the buzzer is disabled in this build variant.
//  * SD chip select (GPIO0) belongs to SdFat (sdCsWrite); this driver does not
//    touch it while a transfer is in progress. It is released HIGH in
//    deactivate().
//  * RC522 CS (GPIO15) is forced HIGH before the shared pins change owners.
//
// Wiring both ends back:
//  activate()  : SPI.end() -> SCK/MOSI become GPIO outputs (idle low/high)
//  deactivate(): SCK/MOSI/MISO(12) SPECIAL -> SPI.begin() + setHwCs(false)
// so the next RC522 hardware-SPI transaction works without main changes.
// ---------------------------------------------------------------------------

#include <Arduino.h>
#include <SPI.h>
#include <SdFat.h>

#include "Config.h"

namespace pdks {
namespace softsd {

class Driver : public SdSpiBaseClass {
 public:
  Driver() {}

  // SdFat calls setSckSpeed(initRate) before begin(), then begin(config) and
  // finally setSckSpeed(config.maxSck) after card initialisation. Only use
  // config.maxSck as a fallback when no speed was stored yet.
  void begin(SdSpiConfig config) override {
    if (!m_speedSet) {
      m_halfUs = halfPeriod(config.maxSck);
    }
    // Dedicated SD input; never an output in any state.
    pinMode(cfg::PIN_SD_SOFT_MISO, INPUT);
  }

  void activate() override {
    // Keep the RC522 deselected while the shared SCK/MOSI pins are bit-banged.
    digitalWrite(cfg::PIN_RFID_CS, HIGH);
    // Release the HSPI pin mux (SCK14/MOSI13/MISO12 become plain inputs).
    SPI.end();
    // Latch the Mode-0 idle levels before enabling the output drivers, so the
    // pins cannot glitch while changing from input to output.
    digitalWrite(cfg::SPI_SCK_GPIO, LOW);
    digitalWrite(cfg::SPI_MOSI_GPIO, HIGH);
    pinMode(cfg::SPI_SCK_GPIO, OUTPUT);
    pinMode(cfg::SPI_MOSI_GPIO, OUTPUT);
    pinMode(cfg::PIN_SD_SOFT_MISO, INPUT); // GPIO16: input only, always
    // SD CS stays under SdFat control for the whole transaction.
  }

  void deactivate() override {
    // Park the SD card deselected and the shared bus in idle levels.
    digitalWrite(cfg::PIN_SD_CS, HIGH);
    digitalWrite(cfg::SPI_SCK_GPIO, LOW);
    digitalWrite(cfg::SPI_MOSI_GPIO, HIGH);
    pinMode(cfg::PIN_SD_SOFT_MISO, INPUT); // GPIO16: input only, always
    // Hand SCK/MOSI/MISO back to the HSPI peripheral for the RC522.
    pinMode(cfg::SPI_SCK_GPIO, SPECIAL);
    pinMode(cfg::SPI_MOSI_GPIO, SPECIAL);
    pinMode(cfg::SPI_MISO_GPIO, SPECIAL);
    SPI.begin();
    SPI.setHwCs(false); // CS stays software-controlled (GPIO15 untouched)
  }

  uint8_t receive() override { return transfer(0xFF); }

  uint8_t receive(uint8_t* buf, size_t count) override {
    if (!buf) return 1; // nonzero: SdFat treats this as a DMA/transfer error
    for (size_t i = 0; i < count; ++i) {
      buf[i] = transfer(0xFF);
    }
    return 0;
  }

  void send(uint8_t data) override { (void)transfer(data); }

  void send(const uint8_t* buf, size_t count) override {
    if (!buf) return;
    for (size_t i = 0; i < count; ++i) {
      (void)transfer(buf[i]);
    }
  }

  void setSckSpeed(uint32_t maxSck) override {
    m_halfUs = halfPeriod(maxSck);
    m_speedSet = true;
  }

 private:
  // Half SCK period in microseconds, clamped to [2, 1000].
  // 2 us half period = 250 kHz, the fastest rate this driver allows.
  static uint32_t halfPeriod(uint32_t maxSck) {
    if (maxSck == 0) return 2;          // guard against division by zero
    uint32_t half = 500000UL / maxSck;  // 0.5 s in us / Hz
    // Round up: flooring can produce a clock faster than requested.
    if (500000UL % maxSck != 0) ++half;
    if (half < 2) half = 2;
    if (half > 1000) half = 1000;
    return half;
  }

  // Mode 0, MSB first: MOSI changes while SCK is low, data is sampled on the
  // rising edge, SCK returns low after the sample.
  uint8_t transfer(uint8_t out) {
    uint8_t in = 0;
    for (uint8_t mask = 0x80; mask != 0; mask >>= 1) {
      digitalWrite(cfg::SPI_MOSI_GPIO, (out & mask) ? HIGH : LOW);
      delayMicroseconds(m_halfUs);
      digitalWrite(cfg::SPI_SCK_GPIO, HIGH);
      delayMicroseconds(m_halfUs);
      in = (uint8_t)((in << 1) |
                     (digitalRead(cfg::PIN_SD_SOFT_MISO) == HIGH ? 1u : 0u));
      digitalWrite(cfg::SPI_SCK_GPIO, LOW);
    }
    // SdFat also polls card busy with repeated single-byte receive() calls.
    // Yield there too, not only in buffer transfers, always with SCK LOW.
    if (++m_bytesSinceYield == 32) {
      m_bytesSinceYield = 0;
      yield();
    }
    return in;
  }

  uint32_t m_halfUs = 2;
  bool m_speedSet = false;
  uint8_t m_bytesSinceYield = 0;
};

} // namespace softsd
} // namespace pdks
