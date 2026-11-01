#pragma once

// Minimal hardware-SPI mock. Only the calls the soft-SD driver needs to
// release and restore the HSPI pin mux are modelled.

#include <Arduino.h>

class SPIClass {
 public:
  void begin() {
    ++mock::state().spiBeginCount;
    mock::record(mock::EV_SPI_BEGIN, -1, 0);
  }
  void end() {
    ++mock::state().spiEndCount;
    mock::record(mock::EV_SPI_END, -1, 0);
  }
  void setHwCs(bool use) {
    ++mock::state().spiHwCsCount;
    mock::state().spiLastHwCs = use;
    mock::record(mock::EV_SPI_HWCS, -1, use ? 1 : 0);
  }
};

extern SPIClass SPI;
