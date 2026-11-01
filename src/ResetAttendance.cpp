// This translation unit is EMPTY in every normal firmware target.
#if defined(PDKS_ATTENDANCE_RESET_ID)

#include <Adafruit_SSD1306.h>
#include <Arduino.h>
#include <LittleFS.h>
#include <SPI.h>
#include <Wire.h>
#include <string.h>

#include "AttendanceReset.h"
#include "Config.h"
#include "Console.h"
#include "SoftSdCard.h"

static_assert(cfg::ENABLE_SD && cfg::SD_SOFTSPI && cfg::PIN_BUZZER == 1 &&
              !cfg::UART_LOGGING, "Reset image requires the current TX/SD wiring");
#define PDKS_STRINGIFY_INNER(x) #x
#define PDKS_STRINGIFY(x) PDKS_STRINGIFY_INNER(x)
static constexpr char REQUEST[] = PDKS_STRINGIFY(PDKS_ATTENDANCE_RESET_ID);
static Adafruit_SSD1306 oled(cfg::OLED_WIDTH, cfg::OLED_HEIGHT, &Wire, -1);
static bool oledReady = false;

struct ResetIo {
  pdks::reset::Marker marker(const char* path, const char* request) {
    using pdks::reset::Marker;
    if (!LittleFS.exists(path)) return Marker::Missing;
    File file = LittleFS.open(path, "r");
    if (!file) return Marker::Invalid;
    const size_t length = file.size();
    char token[11] = {};
    if (!length || length >= sizeof(token)) { file.close(); return Marker::Invalid; }
    const int read = file.read(reinterpret_cast<uint8_t*>(token), length);
    file.close();
    if (read < 0 || static_cast<size_t>(read) != length) return Marker::Invalid;
    for (size_t i = 0; i < length; ++i)
      if (token[i] < '0' || token[i] > '9') return Marker::Invalid;
    return strcmp(token, request) == 0 ? Marker::Match : Marker::Other;
  }

  bool prepare(const char* request) {
    const size_t length = strlen(request);
    if (!length || length > 10) return false;
    File file = LittleFS.open(pdks::reset::TEMP, "w");
    if (!file) return false;
    const size_t written = file.write(reinterpret_cast<const uint8_t*>(request), length);
    file.flush();
    file.close();
    if (written != length || marker(pdks::reset::TEMP, request) != pdks::reset::Marker::Match)
      return false;
    return LittleFS.rename(pdks::reset::TEMP, pdks::reset::PENDING);
  }

  bool clearSdEvents() { return pdks::softsd::resetEvents(); }

  bool removeFile(const char* path) {
    if (LittleFS.exists(path) && !LittleFS.remove(path)) return false;
    return !LittleFS.exists(path);
  }

  bool commit() { return LittleFS.rename(pdks::reset::PENDING, pdks::reset::DONE); }
};

static void message(const char* a, const char* b, const char* c, const char* d) {
  if (!oledReady) return;
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 0);
  oled.println(a); oled.println(b); oled.println(c); oled.println(d);
  oled.display();
}

void setup() {
  // Maintenance performs no card scanning, networking, UART writes or clock set.
  pdks::console::begin(115200);
  digitalWrite(cfg::PIN_BUZZER, HIGH);
  pinMode(cfg::PIN_BUZZER, OUTPUT);
  digitalWrite(cfg::PIN_LED_SIGNAL, HIGH);
  pinMode(cfg::PIN_LED_SIGNAL, OUTPUT);
  pinMode(cfg::PIN_SD_SOFT_MISO, INPUT);
  digitalWrite(cfg::PIN_RFID_CS, HIGH);
  pinMode(cfg::PIN_RFID_CS, OUTPUT);
  digitalWrite(cfg::PIN_SD_CS, HIGH);
  pinMode(cfg::PIN_SD_CS, OUTPUT);
  Wire.begin(cfg::PIN_I2C_SDA, cfg::PIN_I2C_SCL);
  Wire.setClock(cfg::I2C_CLOCK_HZ);
  oledReady = oled.begin(SSD1306_SWITCHCAPVCC, cfg::OLED_ADDR);
  message("BAKIM", "KAYITLAR SILINIYOR", "USB'YI CIKARMAYIN", "");

  LittleFSConfig config;
  config.setAutoFormat(false);
  if (!LittleFS.setConfig(config) || !LittleFS.begin()) {
    message("SILME BASARISIZ", "FS ACILAMADI", "FORMAT YAPILMADI", "BAKIM GEREKIYOR");
    return;
  }
  SPI.begin();
  SPI.setHwCs(false);
  // Even if SD mount fails, run() can report a previously completed request.
  pdks::softsd::begin();
  ResetIo io;
  const pdks::reset::Result result = pdks::reset::run(io, REQUEST);
  if (result == pdks::reset::Result::Failed) {
    message("SILME BASARISIZ", "SD / FS KONTROL EDIN", "ISLEM TAMAMLANMADI", "BAKIM GEREKIYOR");
  } else if (result == pdks::reset::Result::AlreadyDone) {
    message("ONCEDEN SILINDI", "TEKRAR SILINMEDI", "WiFi KORUNDU", "NORMAL SURUM BEKLE");
  } else {
    message("PERSONEL SILINDI", "GECMIS SILINDI", "WiFi KORUNDU", "NORMAL SURUM BEKLE");
  }
}

void loop() { delay(10); }

#endif // PDKS_ATTENDANCE_RESET_ID
