#include "Network.h"
#include "Config.h"
#include "Storage.h"
#include "DeviceAuth.h"
#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <LittleFS.h>
#include <string.h>

namespace pdks {
namespace network {
namespace {
constexpr char PATH[] = "/wifi.json";
constexpr char TMP_PATH[] = "/wifi.tmp";
constexpr uint32_t APPLY_DELAY = 600;
constexpr uint32_t RETRY_MS = 30000;
constexpr uint32_t CONNECT_MS = 15000;
constexpr uint32_t SETUP_AP_MS = 5UL * 60UL * 1000UL;
Settings g_settings;
bool g_configOk = true, g_pending = false, g_attempted = false, g_wasConnected = false;
uint32_t g_applyAt = 0, g_lastAttempt = 0;
bool g_apActive = false;
uint32_t g_connectedAt = 0;

void openSetupAp() {
  if (g_apActive) return;
  if (!deviceauth::ready() || !*deviceauth::apSsid() || strlen(deviceauth::apPassword()) < 8) return;
  IPAddress ip(192, 168, 4, 1), mask(255, 255, 255, 0);
  // APConfig/softAP enable only the AP bit; station credentials are preserved.
  if (WiFi.softAPConfig(ip, ip, mask))
    g_apActive = WiFi.softAP(deviceauth::apSsid(), deviceauth::apPassword());
}

bool readFile(const char* path, Settings& settings) {
  File file = LittleFS.open(path, "r");
  if (!file || file.size() == 0 || file.size() > SETTINGS_MAX) return false;
  char json[SETTINGS_MAX + 1] = {};
  size_t length = file.size();
  size_t read = file.readBytes(json, length);
  file.close();
  return read == length && decode(json, length, settings);
}

void queueApply() {
  g_pending = true;
  g_applyAt = millis() + APPLY_DELAY;
}
} // namespace

void begin() {
  g_settings = Settings();
  g_configOk = true;
  g_attempted = false;
  g_wasConnected = false;
  g_apActive = false;
  // Never store SDK credentials in its unrelated flash sectors.
  WiFi.persistent(false);
  WiFi.mode(WIFI_AP_STA);
  WiFi.setAutoReconnect(false); // retries are bounded by our own loop timer
  WiFi.disconnect(false); // STA only
  openSetupAp();
  if (storage::mounted() && LittleFS.exists(PATH)) g_configOk = readFile(PATH, g_settings);
  // Missing or invalid configuration leaves the setup AP available.
  queueApply();
}

bool save(const Settings& candidate) {
  if (!valid(candidate) || !storage::mounted() || storage::locked() ||
      storage::snapshot().freeBytes < cfg::FS_MIN_FREE_BYTES + SETTINGS_MAX) return false;
  char json[SETTINGS_MAX + 1] = {};
  size_t length = encode(candidate, json, sizeof(json));
  if (!length) return false;
  File file = LittleFS.open(TMP_PATH, "w");
  if (!file) return false;
  size_t written = file.write(reinterpret_cast<const uint8_t*>(json), length);
  file.flush();
  file.close();
  Settings verified;
  if (written != length || !readFile(TMP_PATH, verified) ||
      candidate.enabled != verified.enabled || strcmp(candidate.ssid, verified.ssid) ||
      strcmp(candidate.password, verified.password)) return false;
  // LittleFS atomic rename; never remove the old configuration first.
  if (!LittleFS.rename(TMP_PATH, PATH)) return false;
  g_settings = verified;
  g_configOk = true;
  queueApply(); // apply AFTER the POST response, not during HTTP transmission
  return true;
}

void tick(uint32_t now) {
  if (g_pending) {
    if ((int32_t)(now - g_applyAt) < 0) return;
    g_pending = false;
    openSetupAp(); // new settings/forget must never strand the operator
    WiFi.disconnect(false);
    g_attempted = false;
    g_wasConnected = false;
  }
  if (!g_settings.enabled) { openSetupAp(); return; }
  if (WiFi.status() == WL_CONNECTED) {
    if (!g_wasConnected) g_connectedAt = now;
    g_wasConnected = true;
    if (g_apActive && now - g_connectedAt >= SETUP_AP_MS && WiFi.softAPdisconnect(true))
      g_apActive = false; // stop beaconing; do NOT disconnect the station
    return;
  }
  openSetupAp(); // absent/wrong credentials or lost LAN => recovery AP
  if (g_wasConnected) {
    g_wasConnected = false;
    g_lastAttempt = now;
    g_attempted = true;
  }
  if (!g_attempted || now - g_lastAttempt >= RETRY_MS) {
    WiFi.disconnect(false);
    WiFi.begin(g_settings.ssid, g_settings.password); // no blocking wait
    g_lastAttempt = now;
    g_attempted = true;
  }
}

const Settings& settings() { return g_settings; }
bool configOk() { return g_configOk; }
bool connected() { return !g_pending && g_settings.enabled && WiFi.status() == WL_CONNECTED; }
bool setupApActive() { return g_apActive; }
uint32_t setupApSecondsLeft() {
  if (!g_apActive || !connected() || !g_wasConnected) return 0;
  uint32_t elapsed = millis() - g_connectedAt;
  return elapsed >= SETUP_AP_MS ? 0 : (SETUP_AP_MS - elapsed + 999) / 1000;
}
const char* state() {
  if (!g_configOk) return "AYAR DOSYASI OKUNAMADI";
  if (g_pending) return "AYAR UYGULANIYOR";
  if (!g_settings.enabled) return "SIRKET AGI AYARLANMADI";
  if (connected()) return "BAGLANDI";
  if (g_attempted && millis() - g_lastAttempt < CONNECT_MS) return "BAGLANIYOR";
  return "BAGLANAMADI; TEKRAR DENENECEK";
}
} // namespace network
} // namespace pdks
