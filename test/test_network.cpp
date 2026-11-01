#include "Network.h"
#include "Storage.h"
#include "Config.h"
#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <LittleFS.h>
#include <stdio.h>
#include <string.h>
#include <string>

uint32_t mockNow = 0;
MockWiFi WiFi;
MockFS LittleFS;
namespace netmock {
std::map<std::string, std::string> files;
bool failWrite = false, failRead = false, failRename = false;
}
static bool mounted = true, locked = false;
static uint32_t freeBytes = 50000;
namespace pdks { namespace storage {
bool mounted() { return ::mounted; }
bool locked() { return ::locked; }
Snapshot snapshot() { Snapshot s; s.freeBytes = ::freeBytes; return s; }
} }
static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; \
  printf("FAIL line %d: %s\n", __LINE__, #x); } } while (0)

static void tick(uint32_t now) { mockNow = now; pdks::network::tick(now); }

int main() {
  using pdks::network::Settings;
  using pdks::network::SETTINGS_MAX;
  using pdks::network::valid;
  using pdks::network::encode;
  using pdks::network::decode;
  using pdks::network::begin;
  using pdks::network::save;
  using pdks::network::connected;
  using pdks::network::setupApActive;
  using pdks::network::setupApSecondsLeft;
  using pdks::network::configOk;
  Settings settings, output;
  char json[SETTINGS_MAX + 1];
  CHECK(valid(settings));
  size_t length = encode(settings, json, sizeof(json));
  CHECK(length > 0); CHECK(decode(json, length, output)); CHECK(!output.enabled);
  settings.enabled = true;
  strcpy(settings.ssid, "Office test"); strcpy(settings.password, "Test-only-123");
  CHECK(valid(settings));
  length = encode(settings, json, sizeof(json));
  CHECK(length > 0 && length < SETTINGS_MAX);
  CHECK(decode(json, length, output));
  CHECK(strcmp(output.ssid, settings.ssid) == 0);
  CHECK(strcmp(output.password, settings.password) == 0);
  CHECK(encode(settings, json, 8) == 0);
  const char* bad[] = {"{}", "[]", "null", "not json",
    "{\"v\":2,\"enabled\":true,\"ssid\":\"Office\",\"password\":\"password\"}",
    "{\"v\":1,\"enabled\":true,\"ssid\":\"Office\\u0000X\",\"password\":\"password\"}",
    "{\"v\":1,\"enabled\":true,\"ssid\":\"Office\",\"password\":12345678}",
    "{\"v\":1,\"enabled\":false,\"ssid\":\"Office\",\"password\":\"password\"}"};
  for (const char* data : bad) {
    Settings before = output;
    CHECK(!decode(data, strlen(data), output));
    CHECK(strcmp(before.ssid, output.ssid) == 0);
    CHECK(strcmp(before.password, output.password) == 0);
  }
  for (int n = 0; n <= 65; ++n) {
    Settings candidate = settings;
    memset(candidate.password, 0, sizeof(candidate.password));
    memset(candidate.password, 'a', static_cast<size_t>(n));
    CHECK(valid(candidate) == (n == 0 || (n >= 8 && n <= 64)));
  }
  Settings invalid = settings; memset(invalid.ssid, 'x', sizeof(invalid.ssid));
  CHECK(!valid(invalid));
  invalid = settings; strcpy(invalid.ssid, "bad\nname"); CHECK(!valid(invalid));
  invalid = settings; strcpy(invalid.password, "bad\npassword"); CHECK(!valid(invalid));
  invalid = settings; memset(invalid.password, 'z', 64); invalid.password[64] = 0;
  CHECK(!valid(invalid));
  invalid = settings; strcpy(invalid.ssid, "Sirket \xC3\x9C");
  CHECK(valid(invalid));
  length = encode(invalid, json, sizeof(json)); CHECK(decode(json, length, output));
  CHECK(strcmp(invalid.ssid, output.ssid) == 0);
  invalid = settings; memset(invalid.ssid, 's', 32); invalid.ssid[32] = 0;
  strcpy(invalid.password, "quote\\\"test");
  length = encode(invalid, json, sizeof(json)); CHECK(decode(json, length, output));
  CHECK(strcmp(invalid.ssid, output.ssid) == 0);
  CHECK(strcmp(invalid.password, output.password) == 0);
  invalid.password[0] = 0; CHECK(valid(invalid)); // explicit open network
  length = encode(invalid, json, sizeof(json)); CHECK(decode(json, length, output));
  CHECK(output.password[0] == 0);
  std::string withNul(json, length); withNul.push_back('\0');
  CHECK(!decode(withNul.data(), withNul.size(), output));

  netmock::files["/employees.json"] = "roster sentinel";
  netmock::files["/journal.log"] = "journal sentinel";
  begin(); CHECK(WiFi.ap); CHECK(WiFi.modeValue == WIFI_AP_STA);
  CHECK(!WiFi.persistentValue); CHECK(!WiFi.autoReconnect);
  tick(599); CHECK(WiFi.begins == 0);
  tick(600); CHECK(WiFi.begins == 0);
  CHECK(!pdks::network::settings().enabled);
  CHECK(save(settings));
  CHECK(WiFi.begins == 0); // not switched before response delay
  tick(1199); CHECK(WiFi.begins == 0);
  tick(1200); CHECK(WiFi.begins == 1); CHECK(WiFi.ap);
  CHECK(WiFi.ssid == settings.ssid && WiFi.password == settings.password);
  tick(31199); CHECK(WiFi.begins == 1);
  tick(31200); CHECK(WiFi.begins == 2); // missing/wrong network retries, AP survives
  WiFi.state = WL_CONNECTED; tick(32000); CHECK(connected());
  tick(63000); CHECK(WiFi.begins == 2); CHECK(connected());
  WiFi.state = WL_DISCONNECTED; tick(64000); CHECK(!connected());
  tick(93999); CHECK(WiFi.begins == 2);
  tick(94000); CHECK(WiFi.begins == 3);

  const std::string old = netmock::files["/wifi.json"];
  Settings other = settings; strcpy(other.ssid, "Other office");
  netmock::failWrite = true; CHECK(!save(other)); netmock::failWrite = false;
  CHECK(netmock::files["/wifi.json"] == old);
  netmock::failRead = true; CHECK(!save(other)); netmock::failRead = false;
  CHECK(netmock::files["/wifi.json"] == old);
  netmock::failRename = true; CHECK(!save(other)); netmock::failRename = false;
  CHECK(netmock::files["/wifi.json"] == old);
  CHECK(strcmp(pdks::network::settings().ssid, settings.ssid) == 0);
  locked = true; CHECK(!save(other)); locked = false;
  freeBytes = cfg::FS_MIN_FREE_BYTES; CHECK(!save(other)); freeBytes = 50000;
  mounted = false; CHECK(!save(other)); mounted = true;
  // Reboot loads only the committed file, never a failed temporary candidate.
  begin(); CHECK(configOk()); CHECK(strcmp(pdks::network::settings().ssid, settings.ssid) == 0);
  tick(mockNow + 600); CHECK(WiFi.ssid == settings.ssid);
  Settings disabled;
  CHECK(save(disabled)); tick(mockNow + 600);
  CHECK(!pdks::network::settings().enabled); CHECK(WiFi.ap); CHECK(!connected());
  CHECK(netmock::files["/wifi.json"].find(settings.password) == std::string::npos);
  begin(); tick(mockNow + 600); CHECK(!pdks::network::settings().enabled);
  netmock::files["/wifi.json"] = "broken file";
  begin(); tick(mockNow + 600); CHECK(!configOk()); CHECK(WiFi.ap);
  CHECK(!pdks::network::settings().enabled); CHECK(save(settings));
  tick(mockNow + 600); CHECK(configOk());
  CHECK(netmock::files["/employees.json"] == "roster sentinel");
  CHECK(netmock::files["/journal.log"] == "journal sentinel");
  CHECK(!WiFi.everOff);
  // Deferred apply / retries also work over millis wrap.
  mockNow = 0xFFFFFF00u; CHECK(save(settings));
  const int before = WiFi.begins;
  tick(mockNow + 599); CHECK(WiFi.begins == before);
  tick(mockNow + 1); CHECK(WiFi.begins == before + 1);
  // AP shuts down only after five continuously connected minutes, without
  // touching the station connection or credentials.
  WiFi.state = WL_CONNECTED;
  const uint32_t joinedAt = mockNow;
  tick(joinedAt); CHECK(setupApActive()); CHECK(setupApSecondsLeft() == 300);
  const int stationDisconnects = WiFi.disconnects, stationBegins = WiFi.begins;
  const int oldApStops = WiFi.apStops;
  tick(joinedAt + 299999); CHECK(WiFi.ap); CHECK(setupApSecondsLeft() == 1);
  tick(joinedAt + 300000); CHECK(!WiFi.ap); CHECK(!setupApActive());
  CHECK(setupApSecondsLeft() == 0); CHECK(connected());
  CHECK(WiFi.disconnects == stationDisconnects); CHECK(WiFi.begins == stationBegins);
  CHECK(WiFi.modeValue == 1); CHECK(WiFi.apStops == oldApStops + 1);
  CHECK(WiFi.ssid == settings.ssid && WiFi.password == settings.password);
  tick(joinedAt + 600000); CHECK(WiFi.apStops == oldApStops + 1);
  // Forgetting a profile via the LAN reopens the setup AP after the response.
  CHECK(save(disabled)); tick(mockNow + 599); CHECK(!WiFi.ap);
  tick(mockNow + 1); CHECK(WiFi.ap); CHECK(setupApActive()); CHECK(!connected());
  CHECK(setupApSecondsLeft() == 0);
  tick(mockNow + 600000); CHECK(WiFi.ap); // no configured network => never close
  CHECK(save(settings)); tick(mockNow + 600);
  WiFi.state = WL_CONNECTED; tick(mockNow + 1);
  tick(mockNow + 300000); CHECK(!WiFi.ap); CHECK(connected());
  // Losing the station immediately restores AP, and reconnect starts a new 5m.
  const int oldApStarts = WiFi.apStarts;
  WiFi.state = WL_DISCONNECTED; tick(mockNow + 1);
  CHECK(WiFi.ap); CHECK(WiFi.apStarts == oldApStarts + 1);
  CHECK(!connected()); CHECK(setupApSecondsLeft() == 0);
  tick(mockNow + 600000); CHECK(WiFi.ap); // unavailable LAN never closes AP
  WiFi.state = WL_CONNECTED; tick(mockNow + 1);
  CHECK(setupApSecondsLeft() == 300);
  tick(mockNow + 299999); CHECK(WiFi.ap);
  tick(mockNow + 1); CHECK(!WiFi.ap); CHECK(connected());
  // Countdown survives millis wrap, just like the reconnect/apply timers.
  WiFi.state = WL_DISCONNECTED; tick(0xFFFFFF00u); CHECK(WiFi.ap);
  WiFi.state = WL_CONNECTED; tick(0xFFFFFF10u);
  tick(0xFFFFFF10u + 299999u); CHECK(WiFi.ap);
  tick(0xFFFFFF10u + 300000u); CHECK(!WiFi.ap); CHECK(connected());
  begin(); CHECK(WiFi.ap); tick(mockNow + 600);
  WiFi.state = WL_CONNECTED; tick(mockNow + 1);
  CHECK(setupApSecondsLeft() == 300); // reboot offers a fresh setup window
  CHECK(!WiFi.everOff);
  printf("Wi-Fi settings/network: %d checks, %d failure(s)\n", checks, failures);
  return failures ? 1 : 0;
}
