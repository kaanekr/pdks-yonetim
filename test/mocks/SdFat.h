#pragma once

// Minimal SdFat 2.1.1 surface for the host-side driver test. Only the
// external-driver contract (SPI_DRIVER_SELECT=3) is modelled; the real
// SoftSdCard.cpp is intentionally not linked in this test.

#include <stddef.h>
#include <stdint.h>

typedef uint8_t SdCsPin_t;

const uint8_t SHARED_SPI = 0;

class SdSpiConfig;

class SdSpiBaseClass {
 public:
  virtual void activate() {}
  virtual void begin(SdSpiConfig config) = 0;
  virtual void deactivate() {}
  virtual uint8_t receive() = 0;
  virtual uint8_t receive(uint8_t* buf, size_t count) = 0;
  virtual void send(uint8_t data) = 0;
  virtual void send(const uint8_t* buf, size_t count) = 0;
  virtual void setSckSpeed(uint32_t maxSck) { (void)maxSck; }
};

typedef SdSpiBaseClass SpiPort_t;

class SdSpiConfig {
 public:
  SdSpiConfig(SdCsPin_t cs, uint8_t opt, uint32_t maxSpeed, SpiPort_t* port)
      : csPin(cs), options(opt), maxSck(maxSpeed), spiPort(port) {}

  const SdCsPin_t csPin;
  const uint8_t options;
  const uint32_t maxSck;
  SpiPort_t* spiPort;
};
