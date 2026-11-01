#pragma once

// Serial console facade.
//
// With PDKS_UART_LOGGING enabled every call is forwarded to HardwareSerial
// unchanged. With logging disabled (the TX buzzer build shares UART0 TX /
// GPIO1 with the buzzer) begin() only releases the UART so the caller can take
// the pin over, and every input/output entry point becomes a no-op: no
// Serial.begin(), no writes, no reads. This header never configures GPIOs; the
// caller prepares the pin after begin() returns.

#include <Arduino.h>

#include "Config.h"

namespace pdks {
namespace console {

inline void begin(unsigned long baud) {
#if PDKS_UART_LOGGING
  Serial.begin(baud);
#else
  // The requested baud rate is irrelevant: the UART must not be started.
  (void)baud;
  Serial.end();
#endif
}

inline void end() { Serial.end(); }

inline int available() {
#if PDKS_UART_LOGGING
  return Serial.available();
#else
  return 0;
#endif
}

inline int read() {
#if PDKS_UART_LOGGING
  return Serial.read();
#else
  return -1;
#endif
}

inline void println() {
#if PDKS_UART_LOGGING
  Serial.println();
#endif
}

template <class T>
inline void println(const T& value) {
#if PDKS_UART_LOGGING
  Serial.println(value);
#else
  (void)value;
#endif
}

template <class... Args>
inline void printf(const char* format, Args... args) {
#if PDKS_UART_LOGGING
  Serial.printf(format, args...);
#else
  (void)format; // callers pass a literal; nothing is formatted when disabled
  int ignore[] = {0, ((void)args, 0)...};
  (void)ignore;
#endif
}

} // namespace console
} // namespace pdks
