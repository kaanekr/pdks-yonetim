// PDKS terminal firmware: RC522 + PCF8563 + SSD1306 128x32 + green scan LED +
// active buzzer, optional microSD mirror. Local AP + optional STA, no NTP.
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>
#include <LittleFS.h>
#include <MFRC522.h>
#include <RTClib.h>
#include <SPI.h>
#include <Wire.h>
#include <strings.h>

#include "Attendance.h"
#include "CardGate.h"
#include "Config.h"
#include "Console.h"
#include "DisplayUi.h"
#include "DeviceAuth.h"
#include "FirmwareOta.h"
#include "ReleaseVersion.h"
#include "Network.h"
#include "Storage.h"
#include "WifiPage.h"

#ifndef RANDOM_REG32
#define RANDOM_REG32 ((uint32_t)random(0x7FFFFFFF))
#endif

using pdks::Attendance;
using pdks::Employee;
using pdks::Event;
using pdks::Result;
namespace storage = pdks::storage;
namespace console = pdks::console;

// ---------------------------------------------------------------- state ----
static Adafruit_SSD1306 g_oled(cfg::OLED_WIDTH, cfg::OLED_HEIGHT, &Wire, -1);
static RTC_PCF8563 g_rtc;
static MFRC522 g_rfid(cfg::PIN_RFID_CS, cfg::PIN_RFID_RST);
static ESP8266WebServer g_server(cfg::HTTP_PORT);
static Attendance g_core;
static pdks::CardGate g_cardGate(cfg::RFID_RELEASE_MS, cfg::RFID_RELEASE_SAMPLES);

static bool g_oledOk = false;
static bool g_rfidOk = false;
static bool g_rtcPresent = false;
static bool g_clockOk = false;
static const char* g_clockReason = "BASLATILIYOR";
static bool g_storageHealthy = false;
static uint32_t g_epoch = 0;
static char g_lastUid[21] = {};
static char g_message[32] = "BASLATILIYOR";
static char g_storageNote[24] = "DEPO YOK";
static char g_csrf[33] = {};

static char g_msg1[24] = {};
static char g_msg2[24] = {};
static pdks::display::View g_messageView = pdks::display::View::Notice;
static uint32_t g_msgUntil = 0;
static uint32_t g_ledOnAt = 0;
static bool g_ledPulse = false;
static int g_ledLevel = -1;

static uint32_t g_clockProbeAt = 0;
static uint32_t g_scanAt = 0;
static uint32_t g_drawAt = 0;
static uint32_t g_rfidRetryAt = 0;

struct Beep {
  cfg::BuzzerStep notes[8];
  uint8_t steps;
  uint8_t index;
  bool running;
  uint32_t nextAt;
};
static Beep g_beep = {};
static bool g_buzzerToneRunning = false;

// --------------------------------------------------------------- outputs ---
static void setLed(bool on) {
  int level = on ? 1 : 0;
  if (level == g_ledLevel) return;
  g_ledLevel = level;
  bool high = cfg::LED_ACTIVE_HIGH ? on : !on;
  digitalWrite(cfg::PIN_LED_SIGNAL, high ? HIGH : LOW);
}

static void setBuzzer(bool on, unsigned int hz = cfg::BUZZER_TONE_HZ) {
  if (!cfg::ENABLE_BUZZER) return; // Disabled variants must not drive any pin
  if (on && cfg::BUZZER_TONE_HZ != 0 && hz != 0) {
    tone(cfg::PIN_BUZZER, hz);
    g_buzzerToneRunning = true;
    return;
  }
  if (g_buzzerToneRunning) {
    noTone(cfg::PIN_BUZZER);
    g_buzzerToneRunning = false;
  }
  // ESP8266 noTone() leaves LOW: explicitly restore HIGH for this LOW-trigger
  // module or an active buzzer could remain enabled after the waveform stops.
  bool high = cfg::BUZZER_ACTIVE_HIGH ? on : !on;
  digitalWrite(cfg::PIN_BUZZER, high ? HIGH : LOW);
}

static void beepPlay(const cfg::BuzzerStep* pattern, uint8_t count) {
  if (!cfg::ENABLE_BUZZER) {
    g_beep.running = false;
    return;
  }
  if (count > 8) count = 8;
  if (!pattern) count = 0;
  for (uint8_t i = 0; i < count; ++i) g_beep.notes[i] = pattern[i];
  g_beep.steps = count;
  g_beep.index = 0;
  g_beep.running = count > 0;
  g_beep.nextAt = millis();
  setBuzzer(false);
}

static void beepTick(uint32_t now) {
  if (!g_beep.running) return;
  if ((int32_t)(now - g_beep.nextAt) < 0) return;
  if (g_beep.index >= g_beep.steps) {
    setBuzzer(false);
    g_beep.running = false;
    return;
  }
  const cfg::BuzzerStep& note = g_beep.notes[g_beep.index];
  setBuzzer(note.hz != 0, note.hz);
  g_beep.nextAt = now + note.ms;
  ++g_beep.index;
}

static void ledTick(uint32_t now) {
  if (g_ledPulse && (int32_t)(now - g_ledOnAt) >= (int32_t)cfg::LED_OK_ON_MS)
    g_ledPulse = false;
  setLed(g_ledPulse);
}

static void ledPulse(uint32_t now) {
  g_ledOnAt = now;
  g_ledPulse = true;
  setLed(true);
}

// ----------------------------------------------------------------- clock ---
enum class ClockState : uint8_t { Ok, Missing, Invalid };

static uint8_t bcdValue(uint8_t value) {
  return (value >> 4) * 10 + (value & 15);
}

static bool validBcd(uint8_t value, uint8_t minimum, uint8_t maximum) {
  return (value & 15) <= 9 && (value >> 4) <= 9 &&
         bcdValue(value) >= minimum && bcdValue(value) <= maximum;
}

static ClockState readClock(uint32_t& epochOut) {
  Wire.beginTransmission(cfg::RTC_ADDR);
  bool present = (Wire.endTransmission() == 0);
  if (!present) {
    g_rtcPresent = false;
    g_clockOk = false;
    g_clockReason = "RTC ALGILANMADI";
    return ClockState::Missing;
  }
  if (!g_rtcPresent) {
    g_rtcPresent = g_rtc.begin(&Wire);
    if (!g_rtcPresent) {
      g_clockOk = false;
      g_clockReason = "RTC BASLATILAMADI";
      return ClockState::Missing;
    }
  }
  // RTClib's now() does not expose an I2C read failure. Read a checked snapshot
  // instead, so short transfers / invalid BCD can never create a fake timestamp.
  Wire.beginTransmission(cfg::RTC_ADDR);
  Wire.write((uint8_t)0);
  if (Wire.endTransmission(false) != 0 ||
      Wire.requestFrom(cfg::RTC_ADDR, (uint8_t)9) != 9) {
    while (Wire.available()) Wire.read();
    g_clockOk = false;
    g_clockReason = "RTC OKUNAMADI";
    return ClockState::Missing;
  }
  uint8_t raw[9];
  for (size_t i = 0; i < sizeof(raw); ++i) raw[i] = Wire.read();
  uint8_t sec = raw[2] & 0x7F, minute = raw[3] & 0x7F;
  uint8_t hour = raw[4] & 0x3F, day = raw[5] & 0x3F;
  uint8_t month = raw[7] & 0x1F;
  bool valid = !(raw[0] & 0x20) && !(raw[2] & 0x80) &&
               validBcd(sec, 0, 59) && validBcd(minute, 0, 59) &&
               validBcd(hour, 0, 23) && validBcd(day, 1, 31) &&
               validBcd(month, 1, 12) && validBcd(raw[8], 24, 99);
  if (!valid) {
    g_clockOk = false;
    g_clockReason = (raw[2] & 0x80) ? "SAAT AYARLANMAMIS" :
                    (raw[0] & 0x20) ? "RTC DURDURULMUS" : "TARIH GECERSIZ";
    return ClockState::Invalid;
  }
  DateTime dt(2000 + bcdValue(raw[8]), bcdValue(month), bcdValue(day),
              bcdValue(hour), bcdValue(minute), bcdValue(sec));
  if (!dt.isValid()) {
    g_clockOk = false;
    g_clockReason = "TARIH GECERSIZ";
    return ClockState::Invalid;
  }
  epochOut = dt.unixtime();
  g_epoch = epochOut;
  g_clockOk = true;
  g_clockReason = "OK";
  return ClockState::Ok;
}

static void formatHms(uint32_t epoch, char out[9]) {
  uint32_t s = epoch % 86400UL;
  snprintf(out, 9, "%02u:%02u:%02u", (unsigned)(s / 3600UL),
           (unsigned)((s / 60UL) % 60UL), (unsigned)(s % 60UL));
}

static void formatDate(uint32_t epoch, char out[12]) {
  DateTime dt(epoch);
  snprintf(out, 12, "%04u-%02u-%02u", (unsigned)(dt.year() % 10000),
           (unsigned)(dt.month() % 100), (unsigned)(dt.day() % 100));
}

static void formatDuration(uint32_t seconds, char out[16]) {
  if (seconds >= 3600UL) {
    snprintf(out, 16, "%lu:%02lu:%02lu", (unsigned long)(seconds / 3600UL),
             (unsigned long)((seconds / 60UL) % 60UL),
             (unsigned long)(seconds % 60UL));
  } else {
    snprintf(out, 16, "%02lu:%02lu", (unsigned long)((seconds / 60UL) % 60UL),
             (unsigned long)(seconds % 60UL));
  }
}

// --------------------------------------------------------------- display ---
static void initOled() {
  Wire.beginTransmission(cfg::OLED_ADDR);
  g_oledOk = Wire.endTransmission() == 0 &&
             g_oled.begin(SSD1306_SWITCHCAPVCC, cfg::OLED_ADDR);
  if (!g_oledOk) return;
  g_oled.clearDisplay();
  g_oled.setTextSize(1);
  g_oled.setTextColor(SSD1306_WHITE);
  g_oled.setTextWrap(false);
}

static void utf8ToAscii(const char* in, char* out, size_t cap) {
  if (!out || cap == 0) return;
  size_t o = 0;
  const unsigned char* p = (const unsigned char*)in;
  while (p && *p && o + 1 < cap) {
    unsigned char c = *p;
    if (c < 0x80) {
      out[o++] = (c >= 0x20 && c != 0x7F) ? (char)c : ' ';
      ++p;
      continue;
    }
    size_t seq = 1;
    if ((c & 0xE0) == 0xC0) seq = 2;
    else if ((c & 0xF0) == 0xE0) seq = 3;
    else if ((c & 0xF8) == 0xF0) seq = 4;
    for (size_t i = 1; i < seq; ++i)
      if (!p[i]) seq = 1;
    char mapped = '?';
    if (seq == 2) {
      if (c == 0xC3) {
        switch (p[1]) {
          case 0x9C: mapped = 'U'; break;
          case 0xBC: mapped = 'u'; break;
          case 0x96: mapped = 'O'; break;
          case 0xB6: mapped = 'o'; break;
          case 0x87: mapped = 'C'; break;
          case 0xA7: mapped = 'c'; break;
          default: break;
        }
      } else if (c == 0xC4) {
        switch (p[1]) {
          case 0x9E: mapped = 'G'; break;
          case 0x9F: mapped = 'g'; break;
          case 0xB0: mapped = 'I'; break;
          case 0xB1: mapped = 'i'; break;
          default: break;
        }
      } else if (c == 0xC5) {
        switch (p[1]) {
          case 0x9E: mapped = 'S'; break;
          case 0x9F: mapped = 's'; break;
          default: break;
        }
      }
    }
    out[o++] = mapped;
    p += seq;
  }
  out[o] = '\0';
}

static void renderDisplay() {
  if (!g_oledOk) return;
  pdks::display::Screen screen;
  screen.sdEnabled = storage::sdEnabled();
  screen.sdOk = storage::sdOk();
  char time[9] = {};
  char date[12] = {};
  char dayMonth[6] = {};
  char year[5] = {};
  bool hold = g_msgUntil && (int32_t)(millis() - g_msgUntil) < 0;
  if (!hold) g_msgUntil = 0;
  if (hold) {
    screen.title = g_msg1;
    screen.detail = g_msg2;
    screen.view = g_messageView;
    if (g_messageView == pdks::display::View::Entry ||
        g_messageView == pdks::display::View::Exit) {
      screen.detail = g_msg2 + 6;
    }
  } else if (!g_storageHealthy) {
    screen.view = pdks::display::View::Notice;
    screen.title = "DEPOLAMA HATASI";
    screen.detail = g_storageNote;
    screen.hint = "KAYIT YAPILAMIYOR";
  } else if (!g_clockOk) {
    screen.view = pdks::display::View::Notice;
    screen.title = g_rtcPresent ? "SAAT HATALI" : "RTC YOK";
    screen.detail = g_clockReason;
    screen.hint = g_rtcPresent ? "PANELDEN SAATI AYARLA" : "KABLOLARI KONTROL ET";
  } else if (!g_rfidOk) {
    screen.view = pdks::display::View::Notice;
    screen.title = "RFID ALGILANMADI";
    screen.detail = "KART OKUNAMIYOR";
    screen.hint = "KABLOLARI KONTROL ET";
  } else {
    formatHms(g_epoch, time);
    formatDate(g_epoch, date);
    dayMonth[0] = date[8]; dayMonth[1] = date[9]; dayMonth[2] = '.';
    dayMonth[3] = date[5]; dayMonth[4] = date[6];
    memcpy(year, date, 4);
    screen.time = time;
    screen.date = dayMonth;
    screen.year = year;
  }
  pdks::display::render(g_oled, screen);
}

static void showMessage(const char* line1, const char* line2,
                        pdks::display::View view = pdks::display::View::Notice,
                        uint32_t holdMs = cfg::MSG_HOLD_MS) {
  utf8ToAscii(line1, g_msg1, sizeof(g_msg1));
  utf8ToAscii(line2, g_msg2, sizeof(g_msg2));
  utf8ToAscii(line1, g_message, sizeof(g_message));
  g_messageView = view;
  g_msgUntil = millis() + holdMs;
  renderDisplay();
}

// --------------------------------------------------------------- storage ---
static bool replayCommit(const Event& event, void*) {
  return g_core.commit(event) == Result::Ok;
}

static void initStorage() {
  g_storageHealthy = false;
  if (!storage::mount()) {
    snprintf(g_storageNote, sizeof(g_storageNote), "FS YOK");
    return;
  }
  storage::Snapshot snap = storage::snapshot();
  if (!snap.employeesPresent && snap.journalPresent) {
    snprintf(g_storageNote, sizeof(g_storageNote), "ROSTER YOK");
    return;
  }
  if (storage::loadEmployees(g_core) != storage::Status::Ok) {
    snprintf(g_storageNote, sizeof(g_storageNote), "ROSTER BOZUK");
    return;
  }
  if (storage::replayJournal(&replayCommit, nullptr) != storage::Status::Ok) {
    snprintf(g_storageNote, sizeof(g_storageNote), "JOURNAL BOZUK");
    return;
  }
  if (!storage::sdEnabled() && storage::completePendingRewrite() != storage::Status::Ok) {
    snprintf(g_storageNote, sizeof(g_storageNote), "SILME TEMIZLIK BEKLIYOR");
    return;
  }
  g_storageHealthy = true;
  snprintf(g_storageNote, sizeof(g_storageNote), "DEPO HAZIR");
}

// ------------------------------------------------------------------ rfid ---
static bool initRfid() {
  pinMode(cfg::PIN_RFID_CS, OUTPUT);
  digitalWrite(cfg::PIN_RFID_CS, HIGH);
  g_rfid.PCD_Init();
  delay(4);
  byte version = g_rfid.PCD_ReadRegister(MFRC522::VersionReg);
  g_rfidOk = (version != 0x00 && version != 0xFF);
  if (g_rfidOk) {
    g_rfid.PCD_AntennaOn();
    delay(2);
  }
  return g_rfidOk;
}

static pdks::CardPresence readCardUid(char out[21]) {
  out[0] = '\0';
  // REQA ignores HALTed cards. WUPA sees a held card even after our HaltA,
  // so it cannot be mistaken for removal between repeated RF selections.
  g_rfid.PCD_WriteRegister(MFRC522::TxModeReg, 0x00);
  g_rfid.PCD_WriteRegister(MFRC522::RxModeReg, 0x00);
  g_rfid.PCD_WriteRegister(MFRC522::ModWidthReg, 0x26);
  byte atqa[2];
  byte size = sizeof(atqa);
  MFRC522::StatusCode status = g_rfid.PICC_WakeupA(atqa, &size);
  if (status == MFRC522::STATUS_TIMEOUT) {
    byte version = g_rfid.PCD_ReadRegister(MFRC522::VersionReg);
    if (version == 0x00 || version == 0xFF) {
      g_rfidOk = false;
      return pdks::CardPresence::Uncertain;
    }
    return pdks::CardPresence::Absent;
  }
  if (status != MFRC522::STATUS_OK && status != MFRC522::STATUS_COLLISION)
    return pdks::CardPresence::Uncertain;
  if (!g_rfid.PICC_ReadCardSerial()) {
    g_rfid.PCD_StopCrypto1();
    return pdks::CardPresence::Present; // card answered, UID failure is not absence
  }
  static const char kHex[] = "0123456789ABCDEF";
  char raw[21];
  size_t n = 0;
  for (byte i = 0; i < g_rfid.uid.size && i < 10; ++i) {
    raw[n++] = kHex[(g_rfid.uid.uidByte[i] >> 4) & 0x0F];
    raw[n++] = kHex[g_rfid.uid.uidByte[i] & 0x0F];
  }
  raw[n] = '\0';
  g_rfid.PICC_HaltA();
  g_rfid.PCD_StopCrypto1();
  if (n == 0) return pdks::CardPresence::Present;
  char normalized[21];
  if (!Attendance::normalizeUid(raw, normalized)) return pdks::CardPresence::Present;
  strncpy(out, normalized, 20);
  out[20] = '\0';
  return pdks::CardPresence::Present;
}

static void handleCard(const char* uid) {
  strncpy(g_lastUid, uid, 20);
  g_lastUid[20] = '\0';

  uint32_t probe = 0;
  ClockState clock = readClock(probe);
  uint32_t epoch = (clock == ClockState::Ok) ? probe : 0;

  const Employee* employee = g_core.findByCard(uid);
  if (!employee) {
    beepPlay(cfg::BEEP_ERROR, 5);
    char line2[28];
    snprintf(line2, sizeof(line2), "UID %s", uid);
    showMessage("KART TANIMSIZ", line2);
    console::printf("[CARD] bilinmeyen kart %s\n", uid);
    return;
  }

  char name[24];
  utf8ToAscii(employee->name, name, sizeof(name));

  if (!g_storageHealthy) {
    beepPlay(cfg::BEEP_ERROR, 5);
    showMessage("DEPOLAMA HATASI", "KAYIT YOK");
    return;
  }
  if (clock != ClockState::Ok) {
    beepPlay(cfg::BEEP_ERROR, 5);
    showMessage("SAAT HATALI", "KAYIT RED");
    return;
  }

  Event event;
  Result preview = g_core.preview(uid, epoch, event);
  if (preview == Result::TooSoon) {
    beepPlay(cfg::BEEP_WARN, 1);
    showMessage("TEKRAR BEKLE", name);
    return;
  }
  if (preview != Result::Ok) {
    beepPlay(cfg::BEEP_ERROR, 5);
    showMessage("KAYIT RED", pdks::resultText(preview));
    return;
  }

  storage::Status appended = storage::appendEvent(event);
  if (appended != storage::Status::Ok) {
    g_storageHealthy = false;
    snprintf(g_storageNote, sizeof(g_storageNote), "%s",
             storage::statusText(appended));
    beepPlay(cfg::BEEP_ERROR, 5);
    showMessage("DEPOLAMA HATASI", "KAYIT YOK");
    console::printf("[STORAGE] append red: %s\n", storage::statusText(appended));
    return;
  }
  if (g_core.commit(event) != Result::Ok) {
    g_storageHealthy = false;
    snprintf(g_storageNote, sizeof(g_storageNote), "TUTARSIZ");
    beepPlay(cfg::BEEP_ERROR, 5);
    showMessage("KAYIT TUTARSIZ", "DEPOLAMA KILIT");
    return;
  }

  bool entry = (event.type == 'I');
  if (entry) {
    beepPlay(cfg::BEEP_ENTRY, 3);
    char hms[9];
    formatHms(event.epoch, hms);
    char line2[24];
    snprintf(line2, sizeof(line2), "GIRIS %s", hms);
    showMessage(name, line2, pdks::display::View::Entry);
  } else {
    beepPlay(cfg::BEEP_EXIT, 3);
    char spent[16];
    formatDuration(event.seconds, spent);
    char line2[24];
    snprintf(line2, sizeof(line2), "CIKIS %s", spent);
    showMessage(name, line2, pdks::display::View::Exit);
  }
  ledPulse(millis());
  console::printf("[CARD] %s %s seq=%lu sure=%lu\n", entry ? "GIRIS" : "CIKIS",
                uid, (unsigned long)event.sequence,
                (unsigned long)event.seconds);
}

static void scanRfid() {
  if (!g_rfidOk) {
    g_cardGate.observe(pdks::CardPresence::Uncertain, nullptr, millis());
    return;
  }
  char uid[21] = {};
  pdks::CardPresence presence = readCardUid(uid);
  if (g_cardGate.observe(presence, uid, millis())) handleCard(uid);
}

// ------------------------------------------------------------------- web ---
static void sendResult(int code, bool ok, const char* message) {
  char body[192];
  size_t o = 0;
  o += (size_t)snprintf(body + o, sizeof(body) - o, "{\"ok\":%s,\"message\":\"",
                        ok ? "true" : "false");
  for (const char* p = message ? message : ""; *p && o + 5 < sizeof(body); ++p) {
    unsigned char c = (unsigned char)*p;
    if (c == '"' || c == '\\') {
      body[o++] = '\\';
      body[o++] = (char)c;
    } else {
      body[o++] = (c < 0x20) ? ' ' : (char)c;
    }
  }
  o += (size_t)snprintf(body + o, sizeof(body) - o, "\"}");
  g_server.sendHeader("Cache-Control", "no-store");
  g_server.send(code, "application/json", body);
}

static bool requireAuth() {
  if (!pdks::deviceauth::ready()) {
    sendResult(503,false,"OZEL USB KURULUMU GEREKLI; CIHAZ KIMLIK DOSYASI YOK/GECERSIZ");
    return false;
  }
  if (g_server.authenticate(pdks::deviceauth::username(), pdks::deviceauth::password())) return true;
  g_server.requestAuthentication(BASIC_AUTH, "PDKS");
  return false;
}

static bool requireCsrf() {
  const String& token = g_server.header("X-CSRF-Token");
  if (token.length() == 32 && strcmp(token.c_str(), g_csrf) == 0) return true;
  sendResult(403, false, "CSRF GECERSIZ");
  return false;
}

static bool bodyWithinLimit() {
  if (pdks::ota::busy()) { sendResult(409,false,"FIRMWARE GUNCELLEMESI SURUYOR"); return false; }
  if (g_server.arg("plain").length() > cfg::HTTP_MAX_BODY) {
    sendResult(400, false, "ISTEK COK BUYUK");
    return false;
  }
  return true;
}

// Chunked JSON/CSV writer: keeps heap flat (no multi-KB String/DOM).
struct ChunkWriter {
  char buf[320];
  size_t used;

  ChunkWriter() : used(0) { buf[0] = '\0'; }

  void begin(const char* contentType) {
    g_server.setContentLength(CONTENT_LENGTH_UNKNOWN);
    g_server.sendHeader("Cache-Control", "no-store");
    g_server.send(200, contentType, "");
  }
  void flush() {
    if (!used) return;
    g_server.sendContent(buf, used);
    used = 0;
    buf[0] = '\0';
  }
  void add(const char* data, size_t len) {
    while (len) {
      if (used >= sizeof(buf) - 1) flush();
      size_t room = sizeof(buf) - 1 - used;
      size_t take = (len < room) ? len : room;
      memcpy(buf + used, data, take);
      used += take;
      data += take;
      len -= take;
    }
  }
  void add(const char* text) { add(text, strlen(text)); }
  void add(char c) { add(&c, 1); }
  void num(uint32_t v) {
    char t[11];
    snprintf(t, sizeof(t), "%lu", (unsigned long)v);
    add(t);
  }
  void num64(uint64_t v) {
    char t[21];
    snprintf(t, sizeof(t), "%llu", (unsigned long long)v);
    add(t);
  }
  void boolean(bool v) { add(v ? "true" : "false"); }
  void str(const char* text) {
    add('"');
    for (const unsigned char* p = (const unsigned char*)text; p && *p; ++p) {
      unsigned char c = *p;
      if (c == '"' || c == '\\') {
        add('\\');
        add((char)c);
      } else if (c < 0x20) {
        add(' ');
      } else {
        add((char)c);
      }
    }
    add('"');
  }
  void end() {
    flush();
    g_server.sendContent("");
  }
};

// Minimal in-firmware page, used only when /index.html is not on LittleFS.
static const char FALLBACK_HTML[] PROGMEM = R"HTML(<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDKS Kurulum</title>
<style>body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#141414;color:#eee;line-height:1.45}h1{font-size:19px}h2{font-size:15px;margin-top:20px}code{background:#262626;padding:1px 4px;border-radius:3px}#s{background:#1d2b1d;padding:10px;border-radius:6px}.w{color:#f2b36b}</style>
</head><body>
<h1>PDKS Kurulum Sayfasi</h1>
<p class="w">Bu, cihazda arayuz dosyasi yokken gosterilen yedek sayfadir. Tam panel icin <code>data/index.html</code> yukleyin.</p>
<p id="s">Durum yukleniyor...</p>
<h2>Baglanti</h2>
<p><a href="/wifi">Sirket Wi-Fi ayarlarini ac</a></p>
<ul><li>Kendi Config.h / Secrets.h ayarlarinizdaki Wi-Fi ve panel bilgilerini kullanin.</li>
<li>Panel: <code>http://192.168.4.1/</code></li></ul>
<h2>Kurulum adimlari</h2>
<ol><li>Telefon veya bilgisayardan AP'ye baglanin.</li>
<li>Panele girip tarih/saati ayarlayin (RTC saati yalnizca panelden kurulur).</li>
<li>Kart okutup UID ogrenin, calisan ekleyin.</li>
<li>Ayni karti 5 saniye icinde tekrar okutmayin; yeniden okutmak icin once karti cekin.</li></ol>
<h2>Bakim</h2>
<ul><li>Kayitlar LittleFS icinde <code>/journal.log</code> olarak tutulur; otomatik silinmez veya kirpilmaz.</li>
<li><code>pio run -t uploadfs</code> TUM calisan kayitlarini SILER. Yalnizca yedek aldiktan sonra kullanin.</li>
<li>Dosya sistemi acilmazsa once veriyi koruyun. Bilincli ilk kurulumda seri porttan (115200) <code>FORMAT YES</code> yazilabilir; bu komut ulasilamayan eski veriyi de silebilir.</li>
<li>Varsayilan AP ve panel sifresini mutlaka degistirin.</li></ul>
<script>
fetch('/api/status',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
var t=d.clock_ok?new Date(d.now*1000).toISOString().slice(11,19):'--:--:--';
document.getElementById('s').textContent='Saat '+t+' | Depolama '+(d.storage_ok?'OK':'HATA')+' | RFID '+(d.rfid_ok?'OK':'YOK')+' | OLED '+(d.oled_ok?'OK':'YOK')+' | Kayit no '+d.sequence;
}).catch(function(){document.getElementById('s').textContent='Durum alinamadi.';});
</script>
</body></html>)HTML";

static void handleIndex() {
  if (!requireAuth()) return;
  g_server.sendHeader("Cache-Control", "no-store");
  if (storage::mounted() && LittleFS.exists(storage::INDEX_PATH)) {
    File page = LittleFS.open(storage::INDEX_PATH, "r");
    if (page) {
      g_server.streamFile(page, "text/html");
      page.close();
      return;
    }
  }
  g_server.send_P(200, "text/html", FALLBACK_HTML);
}

static void handleWifiPage() {
  if (!requireAuth()) return;
  g_server.sendHeader("Cache-Control", "no-store");
  g_server.send_P(200, "text/html; charset=utf-8", WIFI_HTML);
}

static void handleWifiStatus() {
  if (!requireAuth()) return;
  ChunkWriter out;
  out.begin("application/json");
  out.add("{\"csrf\":"); out.str(g_csrf);
  out.add(",\"configured\":"); out.boolean(pdks::network::settings().enabled);
  out.add(",\"config_ok\":"); out.boolean(pdks::network::configOk());
  out.add(",\"ssid\":"); out.str(pdks::network::settings().ssid);
  out.add(",\"connected\":"); out.boolean(pdks::network::connected());
  out.add(",\"state\":"); out.str(pdks::network::state());
  out.add(",\"ap_ip\":"); out.str(WiFi.softAPIP().toString().c_str());
  out.add(",\"ap_active\":"); out.boolean(pdks::network::setupApActive());
  out.add(",\"ap_seconds_left\":"); out.num(pdks::network::setupApSecondsLeft());
  out.add(",\"station_ip\":");
  out.str(pdks::network::connected() ? WiFi.localIP().toString().c_str() : "");
  out.add("}"); out.end();
}

static void handleWifiSave() {
  if (!requireAuth() || !requireCsrf() || !bodyWithinLimit()) return;
  const String body = g_server.arg("plain");
  pdks::network::Settings candidate;
  if (!pdks::network::decode(body.c_str(), body.length(), candidate) || !candidate.enabled ||
      strcmp(candidate.ssid, pdks::deviceauth::apSsid()) == 0) {
    sendResult(400, false, "AG AYARI GECERSIZ; KURULUM AGINI DEGIL MODEM AGINI GIRIN");
    return;
  }
  if (!pdks::network::save(candidate)) {
    sendResult(503, false, "WIFI AYARI KAYDEDILEMEDI; DEPOLAMAYI KONTROL EDIN");
    return;
  }
  sendResult(200, true, "Ayar kaydedildi; sirket agina baglanma denenecek.");
}

static void handleWifiForget() {
  if (!requireAuth() || !requireCsrf() || !bodyWithinLimit()) return;
  pdks::network::Settings disabled;
  if (!pdks::network::save(disabled)) {
    sendResult(503, false, "WIFI AYARI SILINEMEDI; DEPOLAMAYI KONTROL EDIN");
    return;
  }
  sendResult(200, true, "Sirket agi unutuldu. Kurulum agi ve mesai kayitlari korunur.");
}

static void handleStatus() {
  if (!requireAuth()) return;
  uint32_t now = g_epoch;

  ChunkWriter out;
  out.begin("application/json");
  out.add("{\"csrf\":");
  out.str(g_csrf);
  out.add(",\"now\":");
  out.num(now);
  out.add(",\"clock_ok\":");
  out.boolean(g_clockOk);
  out.add(",\"rtc_present\":");
  out.boolean(g_rtcPresent);
  out.add(",\"clock_reason\":");
  out.str(g_clockReason);
  out.add(",\"storage_ok\":");
  out.boolean(g_storageHealthy && !storage::locked());
  out.add(",\"rfid_ok\":");
  out.boolean(g_rfidOk);
  out.add(",\"oled_ok\":");
  out.boolean(g_oledOk);
  out.add(",\"sd_enabled\":");
  out.boolean(storage::sdEnabled());
  out.add(",\"sd_ok\":");
  out.boolean(storage::sdOk());
  out.add(",\"sd_softspi\":");
  out.boolean(cfg::SD_SOFTSPI);
  out.add(",\"buzzer_enabled\":");
  out.boolean(cfg::ENABLE_BUZZER);
  out.add(",\"buzzer_gpio\":");
  out.num(cfg::PIN_BUZZER);
  out.add(",\"serial_diagnostics\":");
  out.boolean(cfg::UART_LOGGING);
  out.add(",\"last_uid\":");
  out.str(g_lastUid);
  out.add(",\"message\":");
  out.str(g_message);
  out.add(",\"sequence\":");
  out.num(g_core.lastSequence());
  out.add(",\"firmware_version\":"); out.str(pdks::release::VERSION);
  out.add(",\"firmware_sequence\":"); out.num(pdks::release::SEQUENCE);
  out.add(",\"firmware_target\":"); out.str(pdks::release::TARGET);
  out.add(",\"firmware_build\":"); out.str(pdks::release::BUILD);
  out.add(",\"ota_update\":"); out.boolean(PDKS_ENABLE_OTA && pdks::deviceauth::ready());
  out.add(",\"api_version\":5,\"permanent_delete\":true,\"event_names\":true,\"journal_revision\":");
  out.num(storage::journalRevision());
  out.add(",\"clock_recovery\":true,\"personnel_management\":true,\"manual_attendance\":true,\"roster_version\":");
  out.num(g_core.rosterVersion());
  out.add(",\"last_event_epoch\":");
  out.num(g_core.lastEpoch());
  out.add(",\"employees\":[");
  bool firstEmployee = true;
  for (size_t i = 0; i < g_core.count(); ++i) {
    const Employee* e = g_core.at(i);
    if (!e) continue;
    if (!firstEmployee) out.add(',');
    firstEmployee = false;
    out.add("{\"uid\":");
    out.str(e->uid);
    out.add(",\"name\":");
    out.str(e->name);
    out.add(",\"card_uid\":");
    out.str(e->cardUid);
    out.add(",\"active\":");
    out.boolean(e->active);
    out.add(",\"inside\":");
    out.boolean(e->inside);
    out.add(",\"entry\":");
    out.num(e->entryEpoch);
    out.add(",\"total_seconds\":");
    out.num64(e->totalSeconds);
    out.add(",\"live_seconds\":");
    uint32_t live = 0;
    if (e->inside && g_clockOk && now >= e->entryEpoch)
      live = now - e->entryEpoch;
    out.num(live);
    out.add('}');
    if ((i & 0x07) == 0x07) yield();
  }
  out.add("]}");
  out.end();
}

static bool csvRow(const Event& event, void* ctx) {
  static uint16_t rows = 0;
  bool* aborted = (bool*)ctx;
  if (!*aborted && !g_server.client().connected()) *aborted = true;
  // A client cancel is never treated as log corruption: always return true.
  if (*aborted) return true;
  char row[80];
  int n = snprintf(row, sizeof(row), "%lu,%lu,%s,%c,%lu,\"",
                   (unsigned long)event.sequence, (unsigned long)event.epoch,
                   event.uid, event.type, (unsigned long)event.seconds);
  if (n > 0) g_server.sendContent(row, (size_t)n);
  const Employee* employee = g_core.find(event.uid);
  const char* name = event.type == 'R' ? "" : event.name[0] ? event.name : employee ? employee->name : "";
  const char* source = event.type == 'R' ? "unknown" : event.name[0] ?
      (event.nameRecorded ? "recorded" : "migrated") : employee ? "current" : "unknown";
  String text;
  text.reserve(100);
  for (const char* p=name; *p; ++p) { if (*p == '"') text += '"'; text += *p; }
  text += "\","; text += source; text += event.archived ? ",1\n" : ",0\n";
  g_server.sendContent(text);
  if ((++rows & 0x0F) == 0) yield();
  return true;
}

static void handleEventsCsv() {
  if (!requireAuth()) return;
  if (!g_storageHealthy || storage::locked()) {
    sendResult(503, false, "DEPOLAMA HAZIR DEGIL");
    return;
  }
  // Validate the whole log before sending anything (integrity gate).
  if (storage::replayJournal(nullptr, nullptr) != storage::Status::Ok) {
    g_storageHealthy = false;
    sendResult(503, false, "JOURNAL BOZUK");
    return;
  }
  g_server.setContentLength(CONTENT_LENGTH_UNKNOWN);
  g_server.sendHeader("Content-Disposition",
                      "attachment; filename=\"pdks-kayitlar.csv\"");
  g_server.sendHeader("Cache-Control", "no-store");
  g_server.send(200, "text/csv; charset=utf-8", "");
  g_server.sendContent("sequence,epoch,uid,event,seconds,name,name_source,archived\n");
  bool aborted = false;
  if (storage::replayJournal(&csvRow, &aborted) != storage::Status::Ok) {
    g_storageHealthy = false;
  }
  g_server.sendContent("");
}

static bool sanitizeName(const char* input, char out[49]) {
  if (!input) return false;
  const char* start = input;
  while (*start == ' ') ++start;
  size_t len = strlen(start);
  while (len && start[len - 1] == ' ') --len;
  if (len == 0 || len > 48) return false;
  size_t o = 0;
  for (size_t i = 0; i < len; ++i) {
    unsigned char c = (unsigned char)start[i];
    if (c < 0x20 || c == 0x7F) return false;
    out[o++] = (char)c;
  }
  out[o] = '\0';
  return true;
}

static bool freshIdentity(const char* requested, char identity[21]) {
  if (!Attendance::normalizeUid(requested, identity)) return false;
  bool used = false;
  if (storage::uidHasHistory(identity, used) != storage::Status::Ok) return false;
  if (!used) return true;
  // A re-issued card must never inherit a deleted person's stable history UID.
  for (unsigned attempt=0; attempt<16; ++attempt) {
    snprintf(identity, 21, "%08lX", (unsigned long)RANDOM_REG32);
    if (!strcmp(identity, "00000000") || g_core.find(identity) || g_core.findByCard(identity)) continue;
    if (storage::uidHasHistory(identity, used) != storage::Status::Ok) return false;
    if (!used) return true;
  }
  return false;
}

static void handleEmployee() {
  if (!requireAuth()) return;
  if (!requireCsrf()) return;
  if (!bodyWithinLimit()) return;
  if (!g_storageHealthy || storage::locked()) {
    sendResult(503, false, "DEPOLAMA HAZIR DEGIL");
    return;
  }

  StaticJsonDocument<384> doc;
  if (deserializeJson(doc, g_server.arg("plain"))) {
    sendResult(400, false, "JSON GECERSIZ");
    return;
  }
  const char* uidIn = doc["uid"] | "";
  const char* nameIn = doc["name"] | "";
  if (!doc["uid"].is<const char*>() || !doc["name"].is<const char*>() ||
      doc["uid"].as<JsonString>().size() != strlen(uidIn) ||
      doc["name"].as<JsonString>().size() != strlen(nameIn)) {
    sendResult(400, false, "METIN GECERSIZ");
    return;
  }
  char uid[21];
  char name[49];
  if (!Attendance::normalizeUid(uidIn, uid)) {
    sendResult(400, false, "UID GECERSIZ");
    return;
  }
  if (!sanitizeName(nameIn, name)) {
    sendResult(400, false, "ISIM GECERSIZ");
    return;
  }

  bool existed = (g_core.find(uid) != nullptr);
  char identity[21];
  if (existed) memcpy(identity, uid, sizeof(identity));
  else if (!freshIdentity(uid, identity)) { sendResult(409,false,"YENI PERSONEL KIMLIGI OLUSTURULAMADI"); return; }
  Attendance* candidate = new Attendance(g_core);
  if (!candidate) {
    sendResult(503, false, "BELLEK YOK");
    return;
  }
  Result checked = existed ? candidate->upsertEmployee(identity, name) : candidate->addEmployee(identity, name, uid);
  if (checked == Result::Full) {
    delete candidate;
    sendResult(409, false, "KAPASITE DOLU (32)");
    return;
  }
  if (checked != Result::Ok) {
    delete candidate;
    sendResult(400, false, pdks::resultText(checked));
    return;
  }
  // Persist first; the live core only changes after a verified disk write.
  storage::Status saved = storage::saveEmployees(*candidate, nullptr);
  if (saved == storage::Status::Ok) g_core = *candidate;
  delete candidate;
  if (saved != storage::Status::Ok) {
    if (saved != storage::Status::BadData) g_storageHealthy = false;
    sendResult(503, false, storage::statusText(saved));
    return;
  }
  sendResult(200, true, existed ? "ISIM GUNCELLENDI" : "CALISAN EKLENDI");
}

static bool currentManagementState(JsonDocument& doc) {
  if (!doc["expected_sequence"].is<uint32_t>() || !doc["roster_version"].is<uint32_t>()) {
    sendResult(400, false, "DURUM SURUMU GEREKLI");
    return false;
  }
  if (!doc["journal_revision"].is<uint32_t>() ||
      doc["journal_revision"].as<uint32_t>() != storage::journalRevision()) {
    sendResult(409, false, "GUNLUK SURUMU DEGISTI/EKSIK; GUNCEL WINDOWS PROGRAMIYLA YENILEYIN");
    return false;
  }
  if (doc["expected_sequence"].as<uint32_t>() != g_core.lastSequence() ||
      doc["roster_version"].as<uint32_t>() != g_core.rosterVersion()) {
    sendResult(409, false, "DURUM DEGISTI; YENILEYIP TEKRAR ONAYLAYIN");
    return false;
  }
  return true;
}

static void handleManualAttendance() {
  if (!requireAuth() || !requireCsrf() || !bodyWithinLimit()) return;
  if (!g_storageHealthy || storage::locked()) { sendResult(503,false,"DEPOLAMA HAZIR DEGIL"); return; }
  StaticJsonDocument<512> doc;
  if (deserializeJson(doc,g_server.arg("plain"))) { sendResult(400,false,"JSON GECERSIZ"); return; }
  const char* uidIn=doc["uid"] | "";
  const char* confirm=doc["confirm"] | "";
  if (!doc["uid"].is<const char*>() || doc["uid"].as<JsonString>().size()!=strlen(uidIn) ||
      !doc["confirm"].is<const char*>() || doc["confirm"].as<JsonString>().size()!=strlen(confirm) ||
      strcmp(confirm,"MANUEL MESAI EKLE")!=0 || !doc["start_epoch"].is<uint32_t>() ||
      !doc["end_epoch"].is<uint32_t>()) {
    sendResult(400,false,"MANUEL MESAI ALANLARI/ONAYI GECERSIZ"); return;
  }
  if (!currentManagementState(doc)) return;
  uint32_t start=doc["start_epoch"].as<uint32_t>(), end=doc["end_epoch"].as<uint32_t>();
  char uid[21];
  if (!Attendance::normalizeUid(uidIn,uid) || start<cfg::MIN_EPOCH || end>cfg::MAX_EPOCH || end<=start) {
    sendResult(400,false,"TARIH/ARALIK GECERSIZ; BITIS BASLANGICTAN SONRA OLMALI"); return;
  }
  uint32_t now=0;
  if (readClock(now)!=ClockState::Ok) { sendResult(409,false,"ONCE CIHAZ SAATINI AYARLAYIN/KURTARIN"); return; }
  if (end>now) { sendResult(409,false,"GELECEK TARIHE TAMAMLANMIS MESAI EKLENEMEZ"); return; }
  Event event;
  Result result=g_core.previewManual(uid,start,end,event);
  if (result!=Result::Ok) { sendResult(409,false,pdks::resultText(result)); return; }
  bool overlap=false;
  storage::Status checked=storage::manualOverlap(uid,start,end,overlap);
  if (checked!=storage::Status::Ok) {
    g_storageHealthy=false; sendResult(503,false,storage::statusText(checked)); return;
  }
  if (overlap) { sendResult(409,false,"MEVCUT MESAI ILE CAKISIYOR; TEKRAR EKLENMEDI"); return; }
  storage::Status saved=storage::appendEvent(event);
  if (saved!=storage::Status::Ok) { g_storageHealthy=false; sendResult(503,false,storage::statusText(saved)); return; }
  if (g_core.commit(event)!=Result::Ok) { g_storageHealthy=false; sendResult(503,false,"MANUEL KAYIT TUTARSIZ"); return; }
  sendResult(200,true,"MANUEL MESAI EKLENDI; ONCEKI KAYITLAR KORUNDU");
}

static void handleEmployeeUpdate() {
  if (!requireAuth() || !requireCsrf() || !bodyWithinLimit()) return;
  if (!g_storageHealthy || storage::locked()) {
    sendResult(503, false, "DEPOLAMA HAZIR DEGIL");
    return;
  }
  StaticJsonDocument<768> doc;
  if (deserializeJson(doc, g_server.arg("plain"))) { sendResult(400,false,"JSON GECERSIZ"); return; }
  if (!currentManagementState(doc)) return;
  const char* uid = doc["uid"] | "";
  const char* card = doc["card_uid"] | "";
  const char* rawName = doc["name"] | "";
  if (!doc["uid"].is<const char*>() || !doc["card_uid"].is<const char*>() ||
      !doc["name"].is<const char*>() || !doc["active"].is<bool>() || !doc["create"].is<bool>() ||
      doc["uid"].as<JsonString>().size()!=strlen(uid) ||
      doc["card_uid"].as<JsonString>().size()!=strlen(card) ||
      doc["name"].as<JsonString>().size()!=strlen(rawName)) {
    sendResult(400,false,"ALAN GECERSIZ"); return;
  }
  char name[49];
  if (!sanitizeName(rawName,name)) { sendResult(400,false,"ISIM GECERSIZ"); return; }
  bool create = doc["create"].as<bool>();
  if (create == (g_core.find(uid)!=nullptr)) {
    sendResult(409,false,create ? "PERSONEL ZATEN VAR" : "PERSONEL BULUNAMADI"); return;
  }
  Attendance* candidate = new Attendance(g_core);
  if (!candidate) { sendResult(503,false,"BELLEK YOK"); return; }
  char identity[21];
  if (!Attendance::normalizeUid(uid,identity) || (create && !freshIdentity(uid,identity))) {
    delete candidate; sendResult(409,false,"PERSONEL KIMLIGI OLUSTURULAMADI"); return;
  }
  Result result = create ? candidate->addEmployee(identity,name,card,doc["active"].as<bool>()) :
      candidate->updateEmployee(identity,name,card,doc["active"].as<bool>());
  if (result!=Result::Ok) {
    delete candidate; sendResult(409,false,pdks::resultText(result)); return;
  }
  storage::Status saved = storage::saveEmployees(*candidate,nullptr);
  if (saved==storage::Status::Ok) g_core = *candidate;
  delete candidate;
  if (saved!=storage::Status::Ok) {
    g_storageHealthy = false; sendResult(503,false,storage::statusText(saved)); return;
  }
  sendResult(200,true,"PERSONEL KAYDEDILDI; GECMIS KORUNDU");
}

static void handlePermanentDelete(bool employee) {
  if (!requireAuth() || !requireCsrf() || !bodyWithinLimit()) return;
  if (!g_storageHealthy || storage::locked()) { sendResult(503,false,"DEPOLAMA HAZIR DEGIL"); return; }
  StaticJsonDocument<512> doc;
  if (deserializeJson(doc,g_server.arg("plain"))) { sendResult(400,false,"JSON GECERSIZ"); return; }
  const char* confirm = doc["confirm"] | "";
  if (!doc["confirm"].is<const char*>() || doc["confirm"].as<JsonString>().size() != strlen(confirm) ||
      strcmp(confirm,employee ? "PERSONELI KALICI SIL" : "OTURUMU KALICI SIL") != 0) {
    sendResult(400,false,"KALICI SILME ICIN ACIK ONAY GEREKLI"); return;
  }
  if (!currentManagementState(doc)) return;
  storage::Status result;
  const uint32_t oldRevision = storage::journalRevision();
  if (employee) {
    const char* uid = doc["uid"] | "";
    if (!doc["uid"].is<const char*>() || doc["uid"].as<JsonString>().size() != strlen(uid)) {
      sendResult(400,false,"PERSONEL UID GECERSIZ"); return;
    }
    result = storage::deleteEmployee(g_core,uid);
  } else {
    if (!doc["target_sequence"].is<uint32_t>() || !doc["target_sequence"].as<uint32_t>()) {
      sendResult(400,false,"KAYIT SIRASI GECERSIZ"); return;
    }
    result = storage::deleteSession(g_core,doc["target_sequence"].as<uint32_t>());
  }
  if (result != storage::Status::Ok) {
    if (storage::locked()) g_storageHealthy = false;
    if (storage::journalRevision() != oldRevision) {
      sendResult(503,false,"SILME ISLENDI; SD/TEMIZLIK BEKLIYOR. TEKRAR SILMEYIN; YENIDEN BASLATIP YENILEYIN.");
    } else sendResult(409,false,"SILINMEDI: TAMAMLANMIS OTURUM/PERSONEL, SD VE BOS ALANI KONTROL EDIN");
    return;
  }
  sendResult(200,true,employee ? "PERSONEL KALICI SILINDI; ISIMLI GECMIS KORUNDU" :
             "SECILEN OTURUM KALICI SILINDI; TOPLAMLAR YENIDEN HESAPLANDI");
}
static void handleEmployeeDelete() { handlePermanentDelete(true); }
static void handleSessionDelete() { handlePermanentDelete(false); }

static void writeRtcEpoch(uint32_t epoch, bool recovered) {
  Wire.beginTransmission(cfg::RTC_ADDR);
  if (Wire.endTransmission() != 0) {
    g_rtcPresent = false;
    g_clockOk = false;
    g_clockReason = "RTC ALGILANMADI";
    sendResult(503, false, recovered ? "OTURUMLAR IPTAL EDILDI; RTC YOK; DURUMU YENILEYIN" : "RTC YOK");
    return;
  }
  if (!g_rtcPresent) g_rtcPresent = g_rtc.begin(&Wire);
  if (!g_rtcPresent) {
    g_clockOk = false;
    g_clockReason = "RTC BASLATILAMADI";
    sendResult(503, false, recovered ? "OTURUMLAR IPTAL EDILDI; RTC BASLATILAMADI; DURUMU YENILEYIN" : "RTC YOK");
    return;
  }
  g_rtc.adjust(DateTime(epoch));
  g_rtc.start();
  uint32_t readback = 0;
  bool readable = readClock(readback) == ClockState::Ok;
  int64_t diff = (int64_t)readback - (int64_t)epoch;
  if (!readable || diff < -(int64_t)cfg::RTC_VERIFY_TOLERANCE_S || diff > (int64_t)cfg::RTC_VERIFY_TOLERANCE_S) {
    g_clockOk = false;
    g_clockReason = "RTC YAZILAMADI";
    sendResult(503,false,recovered ? "OTURUMLAR IPTAL EDILDI; RTC YAZILAMADI; DURUMU YENILEYIN" : "RTC YAZILAMADI");
    return;
  }
  g_epoch = readback;
  g_clockOk = true;
  showMessage("SAAT AYARLANDI", "");
  sendResult(200,true,recovered ? "SAAT KURTARILDI; ACIK OTURUMLAR SURESIZ IPTAL; GECMIS KORUNDU" : "SAAT AYARLANDI");
}

static void handleTimeRecover() {
  if (!requireAuth() || !requireCsrf() || !bodyWithinLimit()) return;
  if (!g_storageHealthy || storage::locked()) { sendResult(503,false,"DEPOLAMA HAZIR DEGIL"); return; }
  StaticJsonDocument<384> doc;
  if (deserializeJson(doc,g_server.arg("plain"))) { sendResult(400,false,"JSON GECERSIZ"); return; }
  const char* confirm = doc["confirm"] | "";
  if (!doc["confirm"].is<const char*>() || doc["confirm"].as<JsonString>().size()!=strlen(confirm) ||
      strcmp(confirm,"IPTAL VE SAAT KURTAR")!=0 || !doc["epoch"].is<uint32_t>()) {
    sendResult(400,false,"ACIK OTURUMLARI IPTAL ONAYI GEREKLI"); return;
  }
  if (!currentManagementState(doc)) return;
  uint32_t epoch = doc["epoch"].as<uint32_t>();
  if (epoch<cfg::MIN_EPOCH || epoch>cfg::MAX_EPOCH) { sendResult(400,false,"TARIH 2024-2099 OLMALI"); return; }
  // Probe hardware before any durable change. Flash and RTC are not one transaction:
  // after R is durable, a failed RTC write leaves a visible, retryable recovery.
  Wire.beginTransmission(cfg::RTC_ADDR);
  if (Wire.endTransmission()!=0) {
    g_rtcPresent = false; g_clockOk = false; g_clockReason = "RTC ALGILANMADI";
    sendResult(503,false,"RTC YOK; KAYIT DEGISTIRILMEDI"); return;
  }
  Event recovery;
  if (g_core.previewRecovery(epoch,recovery)!=Result::Ok) { sendResult(409,false,"KURTARMA HAZIRLANAMADI"); return; }
  storage::Status saved = storage::appendEvent(recovery);
  if (saved!=storage::Status::Ok) {
    g_storageHealthy = false; sendResult(503,false,storage::statusText(saved)); return;
  }
  if (g_core.commit(recovery)!=Result::Ok) {
    g_storageHealthy = false; sendResult(503,false,"KURTARMA KAYDI TUTARSIZ"); return;
  }
  g_clockOk = false;
  writeRtcEpoch(epoch,true);
}

static void handleTime() {
  if (!requireAuth()) return;
  if (!requireCsrf()) return;
  if (!bodyWithinLimit()) return;

  StaticJsonDocument<128> doc;
  if (deserializeJson(doc, g_server.arg("plain"))) {
    sendResult(400, false, "JSON GECERSIZ");
    return;
  }
  JsonVariant value = doc["epoch"];
  if (!value.is<uint32_t>()) {
    sendResult(400, false, "EPOCH GECERSIZ");
    return;
  }
  uint32_t epoch = value.as<uint32_t>();
  if (epoch < cfg::MIN_EPOCH || epoch > cfg::MAX_EPOCH) {
    sendResult(400, false, "TARIH 2024-2099 OLMALI");
    return;
  }
  if (g_core.anyInside()) {
    sendResult(409, false, "ICI OLAN CALISAN VAR");
    return;
  }
  if (epoch < g_core.lastEpoch()) {
    sendResult(409, false, "SAAT GERI ALINAMAZ");
    return;
  }
  writeRtcEpoch(epoch,false);
}

static void handleNotFound() {
  if (!requireAuth()) return;
  sendResult(404, false, "BULUNAMADI");
}

// Firmware-resident test page: does not require uploadfs (which erases logs).
static const char BUZZER_TEST_HTML[] PROGMEM = R"HTML(<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDKS Ses Testi</title><style>body{font-family:system-ui;max-width:640px;margin:30px auto;padding:16px;line-height:1.5}button{font:inherit;padding:12px;margin:6px}#result{min-height:3em}</style></head>
<body><h1>Buzzer testi</h1><p>TX denemesinde I/O ucu TX/GPIO1, VCC 3V, GND G. SD MISO D0'da kalir. RX'e baglamayin. USB'yi cikarmadan kablo degistirmeyin.</p>
<p id="hardware">Durum aliniyor...</p><label for="sound">Ses: </label><select id="sound"><option value="test">Uc notali test</option><option value="entry">Giris</option><option value="exit">Cikis</option><option value="warn">Tekrar okutma uyarisi</option><option value="error">Hata / tanimsiz kart</option></select><button id="play" type="button" disabled>Sesi dene</button><button id="stop" type="button" disabled>Sesi durdur</button>
<p id="result" role="status"></p><p><a href="/">Ana panel</a></p><script>
const hardware=document.getElementById('hardware'),result=document.getElementById('result'),play=document.getElementById('play'),stop=document.getElementById('stop');
let csrf='';
async function status(){const r=await fetch('/api/status',{cache:'no-store',credentials:'same-origin'});if(!r.ok)throw new Error('Durum alinamadi: '+r.status);const d=await r.json();csrf=d.csrf||'';hardware.textContent='Buzzer GPIO '+d.buzzer_gpio+' / '+(d.buzzer_enabled?'acik':'kapali')+' | SD '+(d.sd_ok?'OK':'hazir degil')+' | Seri teshis '+(d.serial_diagnostics?'acik':'kapali');play.disabled=!d.buzzer_enabled||!csrf;stop.disabled=!d.buzzer_enabled||!csrf;}
async function action(path){play.disabled=true;stop.disabled=true;try{await status();if(!csrf)throw new Error('Guvenlik anahtari yok');const r=await fetch(path,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}'});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.message||'Islem reddedildi');result.textContent=d.message;}catch(e){result.textContent=e.message;}finally{try{await status();}catch(e){play.disabled=true;stop.disabled=true;}}}
play.addEventListener('click',()=>action('/api/buzzer/test?sound='+encodeURIComponent(document.getElementById('sound').value)));stop.addEventListener('click',()=>action('/api/buzzer/stop'));status().catch(e=>{result.textContent=e.message;});
</script></body></html>)HTML";

static void handleBuzzerPage() {
  if (!requireAuth()) return;
  g_server.sendHeader("Cache-Control", "no-store");
  g_server.send_P(200, "text/html; charset=utf-8", BUZZER_TEST_HTML);
}

static void handleBuzzerTest() {
  if (!requireAuth() || !requireCsrf() || !bodyWithinLimit()) return;
  if (!cfg::ENABLE_BUZZER) {
    sendResult(503, false, "BUZZER KAPALI");
    return;
  }
  const String sound = g_server.arg("sound");
  if (sound == "entry") beepPlay(cfg::BEEP_ENTRY, 3);
  else if (sound == "exit") beepPlay(cfg::BEEP_EXIT, 3);
  else if (sound == "warn") beepPlay(cfg::BEEP_WARN, 1);
  else if (sound == "error") beepPlay(cfg::BEEP_ERROR, 5);
  else if (sound.length() == 0 || sound == "test") beepPlay(cfg::BEEP_TEST, 5);
  else { sendResult(400, false, "SES SECIMI GECERSIZ"); return; }
  sendResult(200, true, "Ses testi baslatildi");
}

static void handleBuzzerStop() {
  if (!requireAuth() || !requireCsrf() || !bodyWithinLimit()) return;
  if (!cfg::ENABLE_BUZZER) {
    sendResult(503, false, "BUZZER KAPALI");
    return;
  }
  g_beep.running = false;
  setBuzzer(false);
  sendResult(200, true, "Ses durduruldu");
}

// ---------------------------------------------------------------- serial ---
static void printDeviceStatus() {
  uint32_t epoch = 0;
  readClock(epoch); // read-only; never set the RTC from diagnostics
  console::printf("[DURUM] RTC:%s SAAT:%s DEPO:%s RFID:%s OLED:%s SD:%s BUZZER:%s\n",
                g_rtcPresent ? "VAR" : "YOK", g_clockReason,
                g_storageHealthy && !storage::locked() ? "OK" : "HATA",
                g_rfidOk ? "OK" : "YOK", g_oledOk ? "OK" : "YOK",
                !storage::sdEnabled() ? "KAPALI" : storage::sdOk() ? "OK" : "HATA",
                cfg::ENABLE_BUZZER ? "ACIK" : "KAPALI");
  if (cfg::SD_SOFTSPI) console::println("[SD_SOFT] MISO D0/GPIO16 INPUT; RFID MISO D6");
}

static void printI2cDevices() {
  console::println("[I2C] Beklenen: OLED 0x3C, PCF8563 0x51");
  uint8_t count = 0;
  for (uint8_t address = 8; address < 0x78; ++address) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      console::printf("[I2C] Bulundu: 0x%02X\n", (unsigned)address);
      ++count;
    }
    if ((address & 7) == 0) yield();
  }
  console::printf("[I2C] Toplam: %u\n", (unsigned)count);
}

static void handleSerialInput() {
  if (!cfg::UART_LOGGING) return;
  static char line[40];
  static size_t used = 0;
  while (console::available() > 0) {
    char c = (char)console::read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[used] = '\0';
      used = 0;
      if (strcasecmp(line, "FORMAT YES") == 0) {
        storage::Status result = storage::operatorFormat();
        if (result == storage::Status::Ok) {
          g_storageHealthy = false;
          snprintf(g_storageNote, sizeof(g_storageNote), "FORMAT SONRASI");
          showMessage("FORMAT TAMAM", "CIHAZI RESETLEYIN");
          console::println("FORMAT TAMAM. Cihazi yeniden baslatin.");
        } else {
          console::printf("FORMAT RED: %s\n", storage::statusText(result));
        }
      } else if (strcasecmp(line, "STATUS") == 0) {
        printDeviceStatus();
      } else if (strcasecmp(line, "I2C") == 0) {
        printI2cDevices();
      } else if (strcasecmp(line, "BUZZER TEST") == 0) {
        if (!cfg::ENABLE_BUZZER) {
          console::println("[BUZZER] KAPALI: test yapilmadi");
        } else {
          beepPlay(cfg::BEEP_TEST, 5);
          console::printf("[BUZZER] Uc notali test, GPIO%u\n", (unsigned)cfg::PIN_BUZZER);
        }
      } else if (strcasecmp(line, "BUZZER STOP") == 0) {
        g_beep.running = false;
        setBuzzer(false);
        console::println("[BUZZER] DURDURULDU");
      } else if (line[0]) {
        console::println("Komutlar: STATUS, I2C, BUZZER TEST, BUZZER STOP, FORMAT YES");
      }
    } else if (used + 1 < sizeof(line)) {
      line[used++] = c;
    }
  }
}

// ------------------------------------------------------------------ setup --
static void initNetwork() {
  pdks::network::begin();
}

static void buildCsrfToken() {
  uint32_t mix = micros() ^ (uint32_t)ESP.getChipId();
  uint32_t parts[4];
  for (uint8_t i = 0; i < 4; ++i) {
    mix = mix * 1664525UL + 1013904223UL;
    parts[i] = RANDOM_REG32 ^ mix;
  }
  snprintf(g_csrf, sizeof(g_csrf), "%08lx%08lx%08lx%08lx",
           (unsigned long)parts[0], (unsigned long)parts[1],
           (unsigned long)parts[2], (unsigned long)parts[3]);
}

void setup() {
  // Release UART BEFORE taking GPIO1 for a TX buzzer. Never start UART later.
  console::begin(115200);
  // SD input remains input even when a separate TX buzzer is enabled.
  if (cfg::SD_SOFTSPI) pinMode(cfg::PIN_SD_SOFT_MISO, INPUT);
  if (cfg::ENABLE_BUZZER) {
    setBuzzer(false);
    pinMode(cfg::PIN_BUZZER, OUTPUT);
  }
  setLed(false); // preload inactive HIGH before enabling common-anode output
  pinMode(cfg::PIN_LED_SIGNAL, OUTPUT);
  digitalWrite(cfg::PIN_RFID_CS, HIGH);
  pinMode(cfg::PIN_RFID_CS, OUTPUT);
  if (cfg::ENABLE_SD) {
    digitalWrite(cfg::PIN_SD_CS, HIGH);
    pinMode(cfg::PIN_SD_CS, OUTPUT);
  }

  delay(30);
  console::println();
  console::println("PDKS terminal baslatiliyor");

  Wire.begin(cfg::PIN_I2C_SDA, cfg::PIN_I2C_SCL);
  Wire.setClock(cfg::I2C_CLOCK_HZ);

  initOled();
  if (g_oledOk) {
    g_oled.setCursor(0, 0);
    g_oled.print(F("PDKS baslatiliyor"));
    g_oled.display();
  }

  g_rtcPresent = g_rtc.begin(&Wire);
  readClock(g_epoch);

  initStorage();
  SPI.begin();
  SPI.setHwCs(false); // D8 is manual RC522 CS, never hardware-controlled SS
  if (cfg::ENABLE_SD) storage::beginSd();
  if (storage::locked()) g_storageHealthy = false;
  pdks::deviceauth::begin();
  initRfid();
  g_rfidRetryAt = millis();

  initNetwork();
  buildCsrfToken();

  g_server.collectHeaders("X-CSRF-Token", "X-OTA-Token");
  pdks::ota::setup(g_server, g_core, g_csrf, &g_storageHealthy);
  g_server.on("/", HTTP_GET, handleIndex);
  g_server.on("/api/status", HTTP_GET, handleStatus);
  g_server.on("/api/events.csv", HTTP_GET, handleEventsCsv);
  g_server.on("/api/employee", HTTP_POST, handleEmployee);
  g_server.on("/api/employee/update", HTTP_POST, handleEmployeeUpdate);
  g_server.on("/api/employee/delete", HTTP_POST, handleEmployeeDelete);
  g_server.on("/api/attendance/delete", HTTP_POST, handleSessionDelete);
  g_server.on("/api/attendance/manual", HTTP_POST, handleManualAttendance);
  g_server.on("/api/time", HTTP_POST, handleTime);
  g_server.on("/api/time/recover", HTTP_POST, handleTimeRecover);
  g_server.on("/wifi", HTTP_GET, handleWifiPage);
  g_server.on("/api/wifi", HTTP_GET, handleWifiStatus);
  g_server.on("/api/wifi", HTTP_POST, handleWifiSave);
  g_server.on("/api/wifi/forget", HTTP_POST, handleWifiForget);
  g_server.on("/test", HTTP_GET, handleBuzzerPage);
  g_server.on("/api/buzzer/test", HTTP_POST, handleBuzzerTest);
  g_server.on("/api/buzzer/stop", HTTP_POST, handleBuzzerStop);
  g_server.onNotFound(handleNotFound);
  g_server.begin();

  snprintf(g_message, sizeof(g_message), "%s",
           cfg::PIN_BUZZER == 1 ? "SD D0; BUZZER TX; UART KAPALI" :
           cfg::SD_SOFTSPI ? "SD MISO D0; BUZZER KAPALI" : "HAZIR");
  console::printf("AP: %s  http://192.168.4.1/  depo:%s  rfid:%s  oled:%s\n",
                 pdks::deviceauth::apSsid(), g_storageHealthy ? "OK" : g_storageNote,
                g_rfidOk ? "OK" : "YOK", g_oledOk ? "OK" : "YOK");
  if (cfg::UART_LOGGING) {
    printDeviceStatus();
    printI2cDevices();
  }
  if (!g_storageHealthy) {
    console::println("Depolama hazir degil. Bos/bozuk cihazda seri porttan");
    console::println("  FORMAT YES   yazip cihazi yeniden baslatin.");
  }
  renderDisplay();
  if (!pdks::deviceauth::ready()) showMessage("OZEL USB KURULUM", "KIMLIK DOSYASI YOK");
  if (cfg::BOOT_BEEP_TEST && cfg::ENABLE_BUZZER) beepPlay(cfg::BEEP_TEST, 5);
}

void loop() {
  uint32_t now = millis();
  g_server.handleClient();
  pdks::ota::tick();
  if (pdks::ota::busy()) { beepTick(now); ledTick(now); yield(); return; }
  handleSerialInput();
  pdks::network::tick(millis());

  if (now - g_clockProbeAt >= cfg::CLOCK_PROBE_MS) {
    g_clockProbeAt = now;
    uint32_t probe = 0;
    readClock(probe);
  }

  if (now - g_rfidRetryAt >= cfg::RFID_RETRY_MS) {
    g_rfidRetryAt = now;
    if (g_rfidOk) {
      byte version = g_rfid.PCD_ReadRegister(MFRC522::VersionReg);
      g_rfidOk = version != 0x00 && version != 0xFF;
    }
    if (!g_rfidOk) initRfid();
    if (g_oledOk) {
      Wire.beginTransmission(cfg::OLED_ADDR);
      g_oledOk = Wire.endTransmission() == 0;
    } else {
      initOled();
    }
  }

  if (now - g_scanAt >= cfg::RFID_POLL_MS) {
    g_scanAt = now;
    scanRfid();
  }

  beepTick(now);
  ledTick(now);

  if (now - g_drawAt >= cfg::DISPLAY_TICK_MS) {
    g_drawAt = now;
    renderDisplay();
  }

  delay(1);
}
