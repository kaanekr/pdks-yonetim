#pragma once

// Minimal Arduino core mock for the pdks::console host tests.
// It records every HardwareSerial call and pin activity so a test can prove
// both plain forwarding (UART logging on) and complete silence (logging off).

#include <stdint.h>

#include <vector>

#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1

namespace console_mock {

enum EventKind {
  EV_PIN_MODE = 1,
  EV_DIGITAL_WRITE = 2,
  EV_SERIAL_BEGIN = 3,
  EV_SERIAL_END = 4,
  EV_SERIAL_PRINTLN = 5,
  EV_SERIAL_PRINTF = 6,
  EV_SERIAL_WRITE = 7
};

struct Event {
  EventKind kind;
  int pin;
  int value;
};

struct State {
  int serialBegin;
  int serialEnd;
  int serialAvailable;
  int serialRead;
  int serialPrintln;
  int serialPrintf;
  int serialWrite;
  unsigned long lastBaud;
  int availableValue;
  int readValue;
  int pinModeValue[32];
  int pinLevel[32];
  int digitalWrites;
  std::vector<Event> events;

  State()
      : serialBegin(0),
        serialEnd(0),
        serialAvailable(0),
        serialRead(0),
        serialPrintln(0),
        serialPrintf(0),
        serialWrite(0),
        lastBaud(0),
        availableValue(0),
        readValue(-1),
        digitalWrites(0) {
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

inline void record(EventKind kind, int pin, int value) {
  Event event;
  event.kind = kind;
  event.pin = pin;
  event.value = value;
  state().events.push_back(event);
}

} // namespace console_mock

class HardwareSerial {
 public:
  void begin(unsigned long baud) {
    ++console_mock::state().serialBegin;
    console_mock::state().lastBaud = baud;
    console_mock::record(console_mock::EV_SERIAL_BEGIN, -1, 0);
  }

  void end() {
    ++console_mock::state().serialEnd;
    console_mock::record(console_mock::EV_SERIAL_END, -1, 0);
  }

  int available() {
    ++console_mock::state().serialAvailable;
    return console_mock::state().availableValue;
  }

  int read() {
    ++console_mock::state().serialRead;
    return console_mock::state().readValue;
  }

  void println() {
    ++console_mock::state().serialPrintln;
    console_mock::record(console_mock::EV_SERIAL_PRINTLN, -1, 0);
  }

  template <class T>
  void println(const T& value) {
    (void)value;
    ++console_mock::state().serialPrintln;
    console_mock::record(console_mock::EV_SERIAL_PRINTLN, -1, 0);
  }

  void write(uint8_t value) {
    (void)value;
    ++console_mock::state().serialWrite;
    console_mock::record(console_mock::EV_SERIAL_WRITE, -1, 0);
  }

  template <class... Args>
  void printf(const char* format, Args... args) {
    (void)format;
    int ignore[] = {0, ((void)args, 0)...};
    (void)ignore;
    ++console_mock::state().serialPrintf;
    console_mock::record(console_mock::EV_SERIAL_PRINTF, -1, 0);
  }
};

static HardwareSerial Serial;

inline void pinMode(uint8_t pin, uint8_t mode) {
  console_mock::state().pinModeValue[pin & 31u] = (int)mode;
  console_mock::record(console_mock::EV_PIN_MODE, (int)pin, (int)mode);
}

inline void digitalWrite(uint8_t pin, uint8_t value) {
  const int level = value ? HIGH : LOW;
  console_mock::state().pinLevel[pin & 31u] = level;
  ++console_mock::state().digitalWrites;
  console_mock::record(console_mock::EV_DIGITAL_WRITE, (int)pin, level);
}

inline int digitalRead(uint8_t pin) {
  return console_mock::state().pinLevel[pin & 31u];
}
