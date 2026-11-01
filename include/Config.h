#pragma once

// ---------------------------------------------------------------------------
// PDKS build configuration (ESP8266 NodeMCU V3 + RC522 + PCF8563 + SSD1306).
//
// The wiring is fixed. Values below mirror the physical build; changing a pin
// here does not change the board, it only desynchronises firmware and hardware.
//
// Secrets: no secret ships in this file. Optional local overrides can be put
// in include/Secrets.h (git-ignored) which redefines the PDKS_* macros below.
// ---------------------------------------------------------------------------

#include <stddef.h>
#include <stdint.h>

#if defined(__has_include)
#  if __has_include("Secrets.h")
#    include "Secrets.h"
#  endif
#endif

#if defined(ARDUINO_ARCH_ESP8266) && !PDKS_PUBLIC_RELEASE && (!defined(PDKS_LOCAL_CREDENTIALS_CONFIGURED) || !defined(PDKS_ADMIN_PASSWORD) || !defined(PDKS_AP_PASSWORD) || !defined(PDKS_AP_SSID) || !defined(PDKS_ADMIN_USER))
#error "Private bootstrap requires local Secrets.h and PDKS_LOCAL_CREDENTIALS_CONFIGURED=1"
#endif

#ifndef PDKS_AP_SSID
#define PDKS_AP_SSID "PDKS-Setup"
#endif
#ifndef PDKS_AP_PASSWORD
#define PDKS_AP_PASSWORD "TEST-ONLY-AP-CHANGE"
#endif
#ifndef PDKS_ADMIN_USER
#define PDKS_ADMIN_USER "admin"
#endif
#ifndef PDKS_ADMIN_PASSWORD
#define PDKS_ADMIN_PASSWORD "TEST-ONLY-ADMIN-CHANGE"
#endif
#ifndef PDKS_ENABLE_SD
#define PDKS_ENABLE_SD 0
#endif
#ifndef PDKS_SD_SOFTSPI
#define PDKS_SD_SOFTSPI 0
#endif
#ifndef PDKS_ENABLE_BUZZER
#if PDKS_SD_SOFTSPI
#define PDKS_ENABLE_BUZZER 0
#else
#define PDKS_ENABLE_BUZZER 1
#endif
#endif
#ifndef PDKS_BUZZER_GPIO
#define PDKS_BUZZER_GPIO 16
#endif
#ifndef PDKS_UART_LOGGING
#define PDKS_UART_LOGGING 1
#endif
#ifndef PDKS_BOOT_BEEP_TEST
#define PDKS_BOOT_BEEP_TEST 0
#endif

#if !defined(PDKS_PUBLIC_RELEASE) || !PDKS_PUBLIC_RELEASE
static_assert(sizeof(PDKS_AP_PASSWORD) >= 9 && sizeof(PDKS_AP_PASSWORD) <= 64,
              "Wi-Fi password must contain 8..63 bytes");
static_assert(sizeof(PDKS_AP_SSID) >= 2 && sizeof(PDKS_AP_SSID) <= 33,
              "Wi-Fi SSID must contain 1..32 bytes");
static_assert(sizeof(PDKS_ADMIN_PASSWORD) >= 9,
               "Admin password must contain at least 8 bytes");
#endif

namespace cfg {

// ------------------------------- pin map ----------------------------------
constexpr uint8_t PIN_I2C_SDA = 4;   // D2 / GPIO4
constexpr uint8_t PIN_I2C_SCL = 5;   // D1 / GPIO5

constexpr uint8_t SPI_SCK_GPIO = 14;  // D5 / GPIO14
constexpr uint8_t SPI_MISO_GPIO = 12; // D6 / GPIO12
constexpr uint8_t SPI_MOSI_GPIO = 13; // D7 / GPIO13

// GPIO15 must be LOW during reset. RC522 is briefly selected then; the flash
// boot bus is separate. Setup raises CS before starting the shared HSPI bus.
constexpr uint8_t PIN_RFID_CS = 15;   // D8 / GPIO15, software-controlled CS
constexpr uint8_t PIN_RFID_RST = 255; // MFRC522::UNUSED_PIN, RST tied to 3V3

constexpr uint8_t PIN_LED_SIGNAL = 2; // D4 / GPIO2, green cathode via resistor
constexpr uint8_t PIN_BUZZER = PDKS_BUZZER_GPIO; // D0 default; TX/GPIO1 in TX trial
constexpr uint8_t PIN_SD_CS = 0;    // D3 / GPIO0, optional microSD reader
constexpr uint8_t PIN_SD_SOFT_MISO = 16; // D0, dedicated SD input in soft-SPI trial
constexpr bool SD_SOFTSPI = (PDKS_SD_SOFTSPI != 0);
constexpr bool ENABLE_BUZZER = (PDKS_ENABLE_BUZZER != 0);
constexpr bool UART_LOGGING = (PDKS_UART_LOGGING != 0);
constexpr bool BOOT_BEEP_TEST = (PDKS_BOOT_BEEP_TEST != 0);
static_assert(!SD_SOFTSPI || PDKS_ENABLE_SD, "Soft SPI needs SD enabled");
static_assert(!SD_SOFTSPI || !ENABLE_BUZZER || PIN_BUZZER != PIN_SD_SOFT_MISO,
              "D0 cannot be both buzzer output and SD MISO input");
static_assert(PIN_BUZZER == 16 || PIN_BUZZER == 1,
              "Only D0 or TX buzzer wiring is supported in this project");
static_assert(PIN_BUZZER != 1 || !UART_LOGGING,
              "TX buzzer requires UART logging disabled");
static_assert(!BOOT_BEEP_TEST || ENABLE_BUZZER, "Boot sound needs buzzer enabled");

// Common-anode LED: common to 3V3, green cathode via resistor to D4.
// Red cathode stays disconnected. LOW lights green; HIGH turns it off.
constexpr bool LED_ACTIVE_HIGH = false;
// MH-FMD low-level-trigger module: power at 3V3 for a 3V3-safe I/O level.
constexpr bool BUZZER_ACTIVE_HIGH = false;
// Frequency-drive trial for the reported "click only" symptom; the physical
// active/passive type is not confirmed. Set 0 for steady active-buzzer drive.
constexpr uint16_t BUZZER_TONE_HZ = 2000;

// Optional microSD mirror. Disabled by default: internal LittleFS is the only
// authoritative store.
constexpr bool ENABLE_SD = (PDKS_ENABLE_SD != 0);

// ------------------------------ I2C devices -------------------------------
constexpr uint32_t I2C_CLOCK_HZ = 400000;
constexpr uint8_t OLED_ADDR = 0x3C;
constexpr uint8_t OLED_WIDTH = 128;
constexpr uint8_t OLED_HEIGHT = 32;
constexpr uint8_t RTC_ADDR = 0x51; // PCF8563

// -------------------------------- network ---------------------------------
constexpr const char* AP_SSID = PDKS_AP_SSID;
constexpr const char* AP_PASSWORD = PDKS_AP_PASSWORD;
constexpr const char* ADMIN_USER = PDKS_ADMIN_USER;
constexpr const char* ADMIN_PASSWORD = PDKS_ADMIN_PASSWORD;
constexpr uint16_t HTTP_PORT = 80;
constexpr size_t HTTP_MAX_BODY = 1024; // hard request-body ceiling (bytes)

// ------------------------------- persistence ------------------------------
constexpr size_t EMPLOYEES_JSON_MAX = 8192;
constexpr uint32_t FS_MIN_FREE_BYTES = 8192; // append headroom, refuse below
constexpr size_t JOURNAL_MAX_LINE = 256;     // includes hex-encoded immutable name + CRC

// ---------------------------- clocks and limits ---------------------------
// Wall-clock values are local time encoded exactly like DateTime::unixtime().
constexpr uint32_t MIN_EPOCH = 1704067200UL; // 2024-01-01 00:00:00
constexpr uint32_t MAX_EPOCH = 4102444799UL; // 2099-12-31 23:59:59
constexpr uint32_t RTC_VERIFY_TOLERANCE_S = 2;
constexpr uint32_t CLOCK_PROBE_MS = 500; // RTC presence/validity probe period
constexpr uint32_t RFID_POLL_MS = 180;
constexpr uint32_t RFID_RELEASE_MS = 600; // sustained absence before next scan
constexpr uint8_t RFID_RELEASE_SAMPLES = 3; // isolated missed reads never rearm
constexpr uint32_t DISPLAY_TICK_MS = 1000;
constexpr uint32_t RFID_RETRY_MS = 5000;
constexpr uint32_t LED_OK_ON_MS = 800;
constexpr uint32_t MSG_HOLD_MS = 4000;

// -------------------------------- feedback --------------------------------
struct BuzzerStep { uint16_t ms; uint16_t hz; }; // hz=0 is a silent pause
constexpr BuzzerStep BEEP_ENTRY[] = {{90, 1568}, {45, 0}, {130, 2349}};
constexpr BuzzerStep BEEP_EXIT[] = {{140, 2093}, {55, 0}, {180, 1319}};
constexpr BuzzerStep BEEP_WARN[] = {{700, 1100}}; // early repeat: one long warning
constexpr BuzzerStep BEEP_ERROR[] = {{140, 784}, {80, 0}, {140, 784}, {80, 0}, {220, 659}};
constexpr BuzzerStep BEEP_TEST[] = {{130, 1319}, {70, 0}, {130, 1760}, {70, 0}, {180, 2349}};

} // namespace cfg
