#pragma once

// Minimal Arduino core mock for the host-side software-SPI driver test.
// It records pin modes, levels and the transfer bit stream so the test can
// check Mode-0 ordering, MSB-first data, GPIO16 safety and bus hand-back.

#include <stddef.h>
#include <stdint.h>

#include <vector>

#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define SPECIAL 2

namespace mock {

enum EventKind {
  EV_PIN_MODE = 1,
  EV_DIGITAL_WRITE = 2,
  EV_SPI_BEGIN = 3,
  EV_SPI_END = 4,
  EV_SPI_HWCS = 5
};

struct Event {
  EventKind kind;
  int pin;
  int value;
};

struct State {
  std::vector<Event> events;
  int pinModeValue[32];
  int pinLevel[32];
  int output16Count;
  int digitalWrite16Count;
  int yields;
  bool lastYieldSckLow;
  std::vector<unsigned int> delays;
  std::vector<uint8_t> miso; // 0/1 bits fed to GPIO16
  size_t misoPos;
  bool sampledWrongEdge; // GPIO16 read while SCK was not HIGH
  std::vector<uint8_t> sent; // MOSI levels sampled on SCK rising edges
  int spiBeginCount;
  int spiEndCount;
  int spiHwCsCount;
  bool spiLastHwCs;

  State()
      : output16Count(0),
        digitalWrite16Count(0),
        yields(0),
        lastYieldSckLow(false),
        misoPos(0),
        sampledWrongEdge(false),
        spiBeginCount(0),
        spiEndCount(0),
        spiHwCsCount(0),
        spiLastHwCs(true) {
    for (int i = 0; i < 32; ++i) {
      pinModeValue[i] = INPUT;
      pinLevel[i] = LOW;
    }
  }
};

inline State& state() {
  static State s;
  return s;
}

inline void reset() { state() = State(); }

inline void record(int kind, int pin, int value) {
  Event e;
  e.kind = (EventKind)kind;
  e.pin = pin;
  e.value = value;
  state().events.push_back(e);
}

} // namespace mock

inline void pinMode(uint8_t pin, uint8_t mode) {
  mock::state().pinModeValue[pin & 31u] = (int)mode;
  if (pin == 16 && mode != INPUT) ++mock::state().output16Count;
  mock::record(mock::EV_PIN_MODE, (int)pin, (int)mode);
}

inline void digitalWrite(uint8_t pin, uint8_t value) {
  const int level = value ? HIGH : LOW;
  mock::state().pinLevel[pin & 31u] = level;
  if (pin == 16) ++mock::state().digitalWrite16Count;
  if (pin == 14 && level == HIGH) {
    // Rising SCK edge: sample MOSI exactly like the card would.
    mock::state().sent.push_back(
        (uint8_t)(mock::state().pinLevel[13] == HIGH ? 1u : 0u));
  }
  mock::record(mock::EV_DIGITAL_WRITE, (int)pin, level);
}

inline int digitalRead(uint8_t pin) {
  if (pin != 16) return mock::state().pinLevel[pin & 31u];
  mock::State& s = mock::state();
  if (s.pinLevel[14] != HIGH) s.sampledWrongEdge = true;
  if (s.misoPos < s.miso.size()) {
    return s.miso[s.misoPos++] ? HIGH : LOW;
  }
  return HIGH; // idle MISO
}

inline void delayMicroseconds(unsigned int us) {
  mock::state().delays.push_back(us);
}

inline void yield() {
  mock::State& s = mock::state();
  ++s.yields;
  s.lastYieldSckLow = (s.pinLevel[14] == LOW);
}
