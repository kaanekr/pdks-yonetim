// Per-device web/AP credentials. /device-auth.json is the only runtime source.
// A public OTA image ships no fallback secret: a missing or invalid file fails
// closed and is never overwritten. Only a private USB bootstrap build may
// create the file, from cfg:: defaults, through a verified tmp + atomic rename,
// and it only becomes ready after that commit is read back and re-parsed.
// Schema v1 (exactly these four string fields, at most 1024 bytes):
//   {"user":"..","password":"..","ap_ssid":"..","ap_password":".."}
// Limits: user 1..48 (no ':'), password 8..128, SSID 1..32 bytes, AP password
// 8..63 bytes. No control characters and no embedded NUL bytes are accepted.
#include "DeviceAuth.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <stdlib.h>
#include <string.h>

#include "ReleaseVersion.h"

#if PDKS_PUBLIC_RELEASE
// A public image must not carry the compiled fallback strings at all, so
// Config.h (and Storage.h, which pulls it in) stay out of this translation
// unit; only the two storage gates are needed here.
namespace pdks {
namespace storage {
bool mounted();
bool locked();
} // namespace storage
} // namespace pdks
#else
#include "Config.h"
#include "Storage.h"
#endif

namespace pdks {
namespace deviceauth {
namespace {

constexpr const char* kPath = "/device-auth.json";
constexpr size_t kFileMax = 1024;
constexpr size_t kPool = 768;
constexpr size_t kUserMax = 48;
constexpr size_t kPassMin = 8, kPassMax = 128;
constexpr size_t kSsidMax = 32;
constexpr size_t kApPassMax = 63;
#if !PDKS_PUBLIC_RELEASE
constexpr const char* kTmpPath = "/device-auth.tmp";
#endif

struct Credentials {
  char user[kUserMax + 1];
  char pass[kPassMax + 1];
  char ssid[kSsidMax + 1];
  char apPass[kApPassMax + 1];
};

Credentials gCred;   // committed values; getters never copy or allocate
Credentials gParsed; // parse scratch for the attempt in progress
bool gReady = false;

bool plainText(const char* text, size_t len) {
  for (size_t i = 0; i < len; ++i) {
    const unsigned char c = static_cast<unsigned char>(text[i]);
    if (c < 0x20 || c == 0x7f) return false;
  }
  return true;
}

bool acceptField(const char* raw, size_t len, char* out, size_t outSize,
                 size_t minLen, size_t maxLen, bool noColon) {
  if (!raw || len < minLen || len > maxLen || outSize <= len) return false;
  if (!plainText(raw, len)) return false;
  if (noColon && memchr(raw, ':', len)) return false;
  memcpy(out, raw, len);
  out[len] = '\0';
  return true;
}

bool readField(JsonObject obj, const char* key, char* out, size_t outSize,
               size_t minLen, size_t maxLen, bool noColon) {
  const JsonVariant value = obj[key];
  if (!value.is<const char*>()) return false;
  const JsonString text = value.as<JsonString>();
  const char* raw = text.c_str();
  if (!raw || text.size() != strlen(raw)) return false; // embedded NUL
  return acceptField(raw, text.size(), out, outSize, minLen, maxLen, noColon);
}

// Strict schema check; anything unexpected is a rejection, never a repair.
bool parseDocument(char* data, size_t len) {
  if (len == 0 || len > kFileMax) return false;
  for (size_t i = 0; i < len; ++i) {
    const unsigned char c = static_cast<unsigned char>(data[i]);
    if (c == 0 || (c < 0x20 && c != '\t' && c != '\n' && c != '\r')) {
      return false;
    }
  }
  memset(&gParsed, 0, sizeof gParsed);
  DynamicJsonDocument doc(kPool);
  if (deserializeJson(doc, data, len, DeserializationOption::NestingLimit(2))) {
    return false;
  }
  const JsonObject root = doc.as<JsonObject>();
  if (root.isNull() || root.size() != 4) return false;
  return readField(root, "user", gParsed.user, sizeof gParsed.user, 1, kUserMax,
                   true) &&
         readField(root, "password", gParsed.pass, sizeof gParsed.pass,
                   kPassMin, kPassMax, false) &&
         readField(root, "ap_ssid", gParsed.ssid, sizeof gParsed.ssid, 1,
                   kSsidMax, false) &&
         readField(root, "ap_password", gParsed.apPass, sizeof gParsed.apPass,
                   kPassMin, kApPassMax, false);
}

bool readFile(const char* path, char* out, size_t cap, size_t& len) {
  File f = LittleFS.open(path, "r");
  if (!f) return false;
  const size_t size = f.size();
  if (size == 0 || size > cap) {
    f.close();
    return false;
  }
  const size_t got = f.readBytes(out, size);
  f.close();
  if (got != size) return false;
  len = size;
  return true;
}

#if !PDKS_PUBLIC_RELEASE
bool writeFile(const char* path, const char* data, size_t len) {
  File f = LittleFS.open(path, "w");
  if (!f) return false;
  const size_t wrote = f.write(reinterpret_cast<const uint8_t*>(data), len);
  f.flush();
  f.close();
  if (wrote != len) {
    LittleFS.remove(path);
    return false;
  }
  return true;
}

size_t serializeCredentials(char* out, size_t cap) {
  DynamicJsonDocument doc(kPool);
  doc["user"] = gCred.user;
  doc["password"] = gCred.pass;
  doc["ap_ssid"] = gCred.ssid;
  doc["ap_password"] = gCred.apPass;
  if (doc.overflowed()) return 0;
  const size_t needed = measureJson(doc);
  if (needed == 0 || needed > kFileMax || needed >= cap) return 0;
  return serializeJson(doc, out, cap);
}

bool sameParsed() {
  return strcmp(gParsed.user, gCred.user) == 0 &&
         strcmp(gParsed.pass, gCred.pass) == 0 &&
         strcmp(gParsed.ssid, gCred.ssid) == 0 &&
         strcmp(gParsed.apPass, gCred.apPass) == 0;
}
#endif

bool loadExisting(char* buf) {
  size_t len = 0;
  if (!readFile(kPath, buf, kFileMax, len)) return false;
  if (!parseDocument(buf, len)) return false;
  memcpy(&gCred, &gParsed, sizeof gCred);
  gReady = true;
  return true;
}

// First-run migration from the compiled defaults. Private builds only.
bool bootstrap(char* buf) {
#if PDKS_PUBLIC_RELEASE
  (void)buf;
  return false; // public image: no fallback credentials, no writes
#else
  memset(&gCred, 0, sizeof gCred);
  const bool valid =
      acceptField(cfg::ADMIN_USER, strlen(cfg::ADMIN_USER), gCred.user,
                  sizeof gCred.user, 1, kUserMax, true) &&
      acceptField(cfg::ADMIN_PASSWORD, strlen(cfg::ADMIN_PASSWORD), gCred.pass,
                  sizeof gCred.pass, kPassMin, kPassMax, false) &&
      acceptField(cfg::AP_SSID, strlen(cfg::AP_SSID), gCred.ssid,
                  sizeof gCred.ssid, 1, kSsidMax, false) &&
      acceptField(cfg::AP_PASSWORD, strlen(cfg::AP_PASSWORD), gCred.apPass,
                  sizeof gCred.apPass, kPassMin, kApPassMax, false);
  if (!valid) return false;
  const size_t len = serializeCredentials(buf, kFileMax + 1);
  if (!len) return false;
  if (!writeFile(kTmpPath, buf, len)) return false;
  size_t backLen = 0;
  if (!readFile(kTmpPath, buf, kFileMax, backLen) || backLen != len ||
      !parseDocument(buf, backLen) || !sameParsed()) {
    LittleFS.remove(kTmpPath);
    return false;
  }
  if (LittleFS.exists(kPath)) { // never overwrite an existing credential file
    LittleFS.remove(kTmpPath);
    return false;
  }
  if (!LittleFS.rename(kTmpPath, kPath)) {
    LittleFS.remove(kTmpPath);
    return false;
  }
  gReady = true; // only after the commit succeeded
  return true;
#endif
}

} // namespace

bool begin() {
  gReady = false;
  memset(&gCred, 0, sizeof gCred);
  memset(&gParsed, 0, sizeof gParsed);
  if (!storage::mounted() || storage::locked()) return false;
  char* const buf = static_cast<char*>(malloc(kFileMax + 1));
  if (!buf) return false;
  const bool ok =
      LittleFS.exists(kPath) ? loadExisting(buf) : bootstrap(buf);
  free(buf);
  if (!ok) {
    gReady = false;
    memset(&gCred, 0, sizeof gCred);
  }
  return ok;
}

bool ready() { return gReady; }

const char* username() { return gReady ? gCred.user : ""; }
const char* password() { return gReady ? gCred.pass : ""; }
const char* apSsid() { return gReady ? gCred.ssid : ""; }
const char* apPassword() { return gReady ? gCred.apPass : ""; }

} // namespace deviceauth
} // namespace pdks
