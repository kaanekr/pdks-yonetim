#include "WifiSettings.h"
#include <ArduinoJson.h>
#include <string.h>

namespace pdks {
namespace network {
namespace {
size_t boundedLength(const char* value, size_t cap) {
  size_t i = 0;
  while (i < cap && value[i]) ++i;
  return i;
}
bool printable(const char* value, size_t length) {
  for (size_t i = 0; i < length; ++i)
    if ((unsigned char)value[i] < 0x20 || (unsigned char)value[i] == 0x7F) return false;
  return true;
}
} // namespace

bool valid(const Settings& s) {
  const size_t ssidLen = boundedLength(s.ssid, sizeof(s.ssid));
  const size_t passLen = boundedLength(s.password, sizeof(s.password));
  if (ssidLen >= sizeof(s.ssid) || passLen >= sizeof(s.password)) return false;
  if (!s.enabled) return ssidLen == 0 && passLen == 0;
  if (!ssidLen || !printable(s.ssid, ssidLen)) return false;
  if (!passLen) return true; // explicitly selected open network
  if (passLen == 64) {
    for (size_t i = 0; i < passLen; ++i) {
      char c = s.password[i];
      if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')))
        return false;
    }
    return true;
  }
  if (passLen < 8 || !printable(s.password, passLen)) return false;
  for (size_t i = 0; i < passLen; ++i)
    if ((unsigned char)s.password[i] > 0x7E) return false; // WPA passphrase ASCII
  return true;
}

bool decode(const char* json, size_t length, Settings& out) {
  if (!json || !length || length > SETTINGS_MAX || memchr(json, '\0', length)) return false;
  StaticJsonDocument<384> doc;
  if (deserializeJson(doc, json, length) || !doc.is<JsonObject>() ||
      !doc["v"].is<unsigned int>() || doc["v"].as<unsigned int>() != 1 ||
      !doc["enabled"].is<bool>() || !doc["ssid"].is<const char*>() ||
      !doc["password"].is<const char*>()) return false;
  JsonString ssid = doc["ssid"].as<JsonString>();
  JsonString pass = doc["password"].as<JsonString>();
  if (ssid.size() > 32 || pass.size() > 64 ||
      strlen(ssid.c_str()) != ssid.size() || strlen(pass.c_str()) != pass.size()) return false;
  Settings candidate;
  candidate.enabled = doc["enabled"].as<bool>();
  memcpy(candidate.ssid, ssid.c_str(), ssid.size());
  memcpy(candidate.password, pass.c_str(), pass.size());
  if (!valid(candidate)) return false;
  out = candidate;
  return true;
}

size_t encode(const Settings& settings, char* json, size_t capacity) {
  if (!json || !valid(settings)) return 0;
  StaticJsonDocument<384> doc;
  doc["v"] = 1;
  doc["enabled"] = settings.enabled;
  doc["ssid"] = settings.ssid;
  doc["password"] = settings.password;
  size_t length = measureJson(doc);
  if (doc.overflowed() || length >= capacity || length > SETTINGS_MAX) return 0;
  return serializeJson(doc, json, capacity);
}
} // namespace network
} // namespace pdks
