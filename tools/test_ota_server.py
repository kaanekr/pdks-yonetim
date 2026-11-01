"""Native guards for the signed-OTA HTTP server state machine.

src/FirmwareOta.cpp is compiled as-is against host fakes for the ESP8266 web
server, Updater and BearSSL, with the pinned ArduinoJson and the real
Attendance/OtaPolicy code. This checks framing, auth, CSRF, state and the
"last four bytes are withheld until finish" invariant only; real crypto and
the framework build are covered separately (tools/test_ota_policy.py,
tools/release_signing.py).

No network, no device, no flash and no credentials are touched.
"""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'.pio/ota-server-tests'
MOCKS = OUT/'mocks'
LIBRARY = ROOT/'.pio/libdeps/nodemcuv2_sd_soft_tx/ArduinoJson/src'

ARDUINO_H = r'''#pragma once
// Host-side Arduino core stand-in for the signed-OTA server tests. Only the
// surface used by src/FirmwareOta.cpp exists; wall time is a controllable global.
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include <string>

#ifndef PROGMEM
#define PROGMEM
#endif
#define HIGH 1
#define LOW 0

using String = std::string;

extern uint32_t testNow;

inline uint32_t millis() { return testNow; }
inline void yield() {}
'''

WEBSERVER_H = r'''#pragma once
// Fake ESP8266WebServer: stores the two handlers, exposes request headers,
// body and multipart upload state, and captures the single JSON response.
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <functional>
#include <map>
#include <string>

#include <Arduino.h>

enum HTTPMethod {
  HTTP_ANY = 0,
  HTTP_GET,
  HTTP_POST,
  HTTP_PUT,
  HTTP_PATCH,
  HTTP_DELETE,
  HTTP_OPTIONS
};

enum HTTPUploadStatus {
  UPLOAD_FILE_START = 0,
  UPLOAD_FILE_WRITE,
  UPLOAD_FILE_END,
  UPLOAD_FILE_ABORTED
};

struct HTTPUpload {
  HTTPUploadStatus status = UPLOAD_FILE_START;
  size_t currentSize = 0;
  uint8_t buf[2048] = {};
  String name;
  String filename;
  String type;
};

class ESP8266WebServer {
 public:
  typedef std::function<void()> THandlerFunction;

  THandlerFunction finalHandler;
  THandlerFunction prepareHandler;
  THandlerFunction uploadHandler;
  HTTPUpload file;
  std::map<std::string, std::string> headers;
  std::string body;
  bool authOk = true;
  int authCount = 0;
  std::string lastUser;
  std::string lastPass;
  int sendCount = 0;
  int headerCount = 0;
  int status = 0;
  std::string contentType;
  std::string response;
  std::string lastHeaderName;
  std::string lastHeaderValue;

  void on(const String&, HTTPMethod, THandlerFunction handler) {
    prepareHandler = handler;
    uploadHandler = THandlerFunction();
  }
  void on(const String&, HTTPMethod, THandlerFunction handler, THandlerFunction uploader) {
    finalHandler = handler;
    uploadHandler = uploader;
  }
  String arg(const String& name) const { return name == "plain" ? body : String(); }
  String header(const String& name) const {
    std::map<std::string, std::string>::const_iterator it = headers.find(name);
    return it == headers.end() ? String() : it->second;
  }
  bool authenticate(const char* user, const char* pass) {
    ++authCount;
    lastUser = user == nullptr ? "" : user;
    lastPass = pass == nullptr ? "" : pass;
    return authOk;
  }
  void sendHeader(const String& name, const String& value) {
    ++headerCount;
    lastHeaderName = name;
    lastHeaderValue = value;
  }
  void send(int code, const char* type, const String& content) {
    ++sendCount;
    status = code;
    contentType = type == nullptr ? "" : type;
    response = content;
  }
  HTTPUpload& upload() { return file; }
};
'''

WIFI_H = r'''#pragma once
// Fake ESP8266WiFi: free sketch space, a deterministic random counter and a
// restart counter (the OTA server must schedule, never restart by itself).
#include <stddef.h>
#include <stdint.h>

class EspClass {
 public:
  uint32_t freeSketchSpace = 1048576;
  uint32_t randomState = 0x12345678u;
  int restartCount = 0;

  uint32_t getFreeSketchSpace() const { return freeSketchSpace; }
  unsigned long random() {
    randomState = randomState * 1664525u + 1013904223u;
    return static_cast<unsigned long>(randomState);
  }
  void restart() { ++restartCount; }
};

extern EspClass ESP;
'''

BEARSSL_H = r'''#pragma once
// Fake BearSSL: the digest is always 32 zero bytes and verification returns a
// test-controlled bool. Crypto correctness is proven outside this harness.
#include <stddef.h>
#include <stdint.h>

namespace BearSSL {

class PublicKey {
 public:
  explicit PublicKey(const char* pem) : pem_(pem) {}
  const char* pem() const { return pem_; }

 private:
  const char* pem_;
};

class HashSHA256 {
 public:
  void begin() { ++beginCount; }
  void add(const void* data, size_t size) {
    (void)data;
    (void)size;
    ++addCount;
  }
  void end() { ++endCount; }
  const uint8_t* hash() const { return digest; }

  uint8_t digest[32] = {};
  int beginCount = 0;
  int addCount = 0;
  int endCount = 0;
};

class SigningVerifier {
 public:
  explicit SigningVerifier(PublicKey* key) : key_(key) {}
  bool verify(const void* hash, const uint8_t* signature, size_t size) {
    ++calls;
    lastHash = hash;
    lastSignature = signature;
    lastSize = size;
    return result;
  }

  bool result = false;
  int calls = 0;
  const void* lastHash = nullptr;
  const uint8_t* lastSignature = nullptr;
  size_t lastSize = 0;

 private:
  PublicKey* key_;
};

}  // namespace BearSSL
'''

UPDATER_H = r'''#pragma once
// Fake Updater: begin() only succeeds after installSignature(), write() counts
// bytes, and end(false) commits ONLY when every announced byte arrived. end(true)
// is refused outright so a production end(true) can never look successful.
#include <stddef.h>
#include <stdint.h>

#include <BearSSLHelpers.h>

#define U_FLASH 0
#define U_SPIFFS 100
#define U_AUTH 200

class UpdaterClass {
 public:
  void reset() {
    expected = 0;
    written = 0;
    writeCalls = 0;
    beginCount = 0;
    commitCount = 0;
    abortCount = 0;
    endTrueCount = 0;
    installCount = 0;
    signatureInstalled = false;
    running_ = false;
    lastChunk = 0;
  }

  void installSignature(BearSSL::HashSHA256* image, BearSSL::SigningVerifier* signer) {
    ++installCount;
    image_ = image;
    signer_ = signer;
    signatureInstalled = image != nullptr && signer != nullptr;
  }

  bool begin(size_t size, int command = U_FLASH, int ledPin = -1, uint8_t ledOn = 0) {
    (void)ledPin;
    (void)ledOn;
    ++beginCount;
    expected = size;
    written = 0;
    writeCalls = 0;
    running_ = signatureInstalled && command == U_FLASH;
    return running_;
  }

  size_t write(uint8_t* data, size_t size) {
    ++writeCalls;
    lastChunk = size;
    if (!running_ || data == nullptr) return 0;
    written += size;
    return size;
  }

  bool end(bool evenIfRemaining = false) {
    if (evenIfRemaining) {
      ++endTrueCount;
      running_ = false;
      ++abortCount;
      return false;
    }
    const bool ok = running_ && signatureInstalled && expected > 0 && written == expected;
    running_ = false;
    if (ok) {
      ++commitCount;
    } else {
      ++abortCount;
    }
    return ok;
  }

  bool isRunning() const { return running_; }

  size_t expected = 0;
  size_t written = 0;
  size_t lastChunk = 0;
  int writeCalls = 0;
  int beginCount = 0;
  int commitCount = 0;
  int abortCount = 0;
  int endTrueCount = 0;
  int installCount = 0;
  bool signatureInstalled = false;

 private:
  bool running_ = false;
  BearSSL::HashSHA256* image_ = nullptr;
  BearSSL::SigningVerifier* signer_ = nullptr;
};

extern UpdaterClass Update;
'''

PGMSPACE_H = r'''#pragma once
// PROGMEM is meaningless on the host; keep the production headers untouched.
#ifndef PROGMEM
#define PROGMEM
#endif
'''

SOURCE = r'''// Generated harness: compiles the real src/FirmwareOta.cpp in this translation
// unit so its anonymous-namespace state can be reset between cases.
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <Arduino.h>
#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>
#include <BearSSLHelpers.h>
#include <Updater.h>
#include <pgmspace.h>

#include "Attendance.h"
#include "Config.h"
#include "DeviceAuth.h"
#include "ReleaseVersion.h"
#include "Storage.h"

#include "../../../src/FirmwareOta.cpp"

// ----------------------------- fake backends -------------------------------
uint32_t testNow = 0;
EspClass ESP;
UpdaterClass Update;

namespace pdks {
namespace storage {
static bool mountState = true;
static bool lockState = false;
static uint32_t revisionState = 7;
static bool replayOk = true;

bool mounted() { return mountState; }
bool locked() { return lockState; }
uint32_t journalRevision() { return revisionState; }
Status replayJournal(EventVisitor, void* ctx) {
  (void)ctx;
  return replayOk ? Status::Ok : Status::IntegrityError;
}
}  // namespace storage
namespace deviceauth {
static bool readyState = true;
static const char* userState = "pdks-test-user";
static const char* passState = "pdks-test-pass";
bool ready() { return readyState; }
const char* username() { return userState; }
const char* password() { return passState; }
}  // namespace deviceauth
}  // namespace pdks

// ------------------------------- test globals ------------------------------
static ESP8266WebServer server;
static pdks::Attendance attendance;
static bool storageHealthy = true;
static int failures = 0;

static const char* const CSRF = "0123456789ABCDEF0123456789ABCDEF";
static const char* const TARGET = "nodemcuv2_sd_soft_tx";
static const char* const VERSION = "1.5.1";
static const uint32_t SEQUENCE = 10501;  // running firmware is 1.5.0 / 10500
static const uint32_t SIZE = 300;
static const uint32_t REVISION = 7;
static const std::string SHA_ZERO(64, '0');
static const std::string SHA_OTHER(64, 'a');
static const std::string SIGNATURE = std::string(342, 'A') + "==";

static void check(bool ok, const char* name) {
  if (!ok) {
    std::printf("FAIL %s\n", name);
    ++failures;
  }
}

static void resetAll() {
  pdks::ota::web = nullptr;
  pdks::ota::core = nullptr;
  pdks::ota::csrf = nullptr;
  pdks::ota::healthy = nullptr;
  pdks::ota::prepared = pdks::ota::Prepared();
  pdks::ota::active = false;
  pdks::ota::ended = false;
  pdks::ota::seen = false;
  pdks::ota::reboot = false;
  pdks::ota::received = 0;
  pdks::ota::restartAt = 0;
  pdks::ota::tailCount = 0;
  pdks::ota::error = nullptr;
  pdks::ota::verifier.calls = 0;
  pdks::ota::verifier.result = true;
  server = ESP8266WebServer();
  attendance.clear();
  testNow = 1000;
  storageHealthy = true;
  pdks::storage::mountState = true;
  pdks::storage::lockState = false;
  pdks::storage::revisionState = REVISION;
  pdks::storage::replayOk = true;
  pdks::deviceauth::readyState = true;
  ESP.freeSketchSpace = 1048576;
  ESP.restartCount = 0;
  Update.reset();
  pdks::ota::setup(server, attendance, CSRF, &storageHealthy);
}

// ------------------------------- request I/O -------------------------------
static void setHeaders(const std::string& csrf, const std::string& token) {
  server.headers["X-CSRF-Token"] = csrf;
  server.headers["X-OTA-Token"] = token;
}

static std::string prepareBody(const std::string& target, const std::string& version,
                               uint32_t sequence, uint32_t size, const std::string& sha,
                               uint32_t expected, uint32_t roster, uint32_t revision,
                               const std::string& confirm, const std::string& signature) {
  char buffer[1200];
  std::snprintf(buffer, sizeof buffer,
                "{\"target\":\"%s\",\"version\":\"%s\",\"sequence\":%u,\"size\":%u,"
                "\"sha256\":\"%s\",\"meta_signature\":\"%s\",\"confirm\":\"%s\","
                "\"expected_sequence\":%u,\"roster_version\":%u,\"journal_revision\":%u}",
                target.c_str(), version.c_str(), static_cast<unsigned>(sequence),
                static_cast<unsigned>(size), sha.c_str(), signature.c_str(), confirm.c_str(),
                static_cast<unsigned>(expected), static_cast<unsigned>(roster),
                static_cast<unsigned>(revision));
  return std::string(buffer);
}

static std::string validBody() {
  return prepareBody(TARGET, VERSION, SEQUENCE, SIZE, SHA_ZERO, attendance.lastSequence(),
                     attendance.rosterVersion(), REVISION, "FIRMWARE GUNCELLE", SIGNATURE);
}

// Sends the prepare request; headers must be set by the caller beforehand.
static void callPrepare(const std::string& body) {
  server.body = body;
  server.prepareHandler();
}

static std::vector<uint8_t> firmware() {
  std::vector<uint8_t> data(SIZE, 0xAA);
  data[0] = 0xE9;
  data[SIZE - 4] = 0x00;
  data[SIZE - 3] = 0x01;
  data[SIZE - 2] = 0x00;
  data[SIZE - 1] = 0x00;
  return data;
}

static void uploadStart(const std::string& name, const std::string& token) {
  server.file.name = name;
  server.file.currentSize = 0;
  server.file.status = UPLOAD_FILE_START;
  server.headers["X-OTA-Token"] = token;
  server.uploadHandler();
}

static void uploadChunk(const std::vector<uint8_t>& data, size_t offset, size_t size) {
  std::memcpy(server.file.buf, &data[offset], size);
  server.file.currentSize = size;
  server.file.status = UPLOAD_FILE_WRITE;
  server.uploadHandler();
}

static void uploadWhole(const std::vector<uint8_t>& data) {
  uploadChunk(data, 0, 99);                 // 95 bytes reach the updater
  uploadChunk(data, 99, data.size() - 99);  // +201 -> 296, tail of 4 held back
}

static void uploadEnd() {
  server.file.status = UPLOAD_FILE_END;
  server.file.currentSize = 0;
  server.uploadHandler();
}

static void uploadAborted() {
  server.file.status = UPLOAD_FILE_ABORTED;
  server.uploadHandler();
}

static std::string token() { return std::string(pdks::ota::prepared.token); }

// ---------------------------------- cases ----------------------------------
static void testValidUpload() {
  resetAll();
  check(!pdks::ota::busy(), "idle server is not busy");
  check(static_cast<bool>(server.uploadHandler), "setup registers the upload handler");

  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 200, "prepare accepted");
  check(server.response == std::string("{\"ok\":true,\"token\":\"") + pdks::ota::prepared.token + "\"}",
        "prepare body carries the token");
  check(server.lastHeaderName == "Cache-Control" && server.lastHeaderValue == "no-store",
        "prepare answers no-store");
  check(pdks::ota::prepared.ready, "prepared ready");
  check(std::strlen(pdks::ota::prepared.token) == 32, "token is 32 chars");
  check(pdks::ota::prepared.size == SIZE, "prepared size");
  check(pdks::ota::prepared.offered == SEQUENCE, "prepared sequence");
  check(pdks::ota::prepared.sequence == 0, "prepared binds core sequence");
  check(pdks::ota::prepared.roster == attendance.rosterVersion(), "prepared binds roster");
  check(pdks::ota::prepared.revision == REVISION, "prepared binds revision");
  check(std::string(pdks::ota::prepared.sha) == SHA_ZERO, "prepared binds digest");
  check(Update.installCount == 1, "signature installed by setup");
  check(Update.beginCount == 0, "prepare never begins the updater");
  check(pdks::ota::verifier.calls == 1, "signature verified once");
  check(pdks::ota::verifier.lastSize == 256, "signature verified at 256 bytes");
  check(pdks::ota::envelopeHash.endCount == 1, "envelope hashed once");
  check(server.authCount == 1, "credentials checked once");
  check(server.lastUser == std::string("pdks-test-user"), "device credentials used");

  const std::string prepared = token();
  const std::vector<uint8_t> data = firmware();
  uploadStart("firmware", prepared);
  check(Update.beginCount == 1, "begin accepted");
  check(Update.isRunning(), "updater running");
  check(Update.expected == SIZE, "updater sized from prepare");
  check(pdks::ota::active, "upload active");
  uploadWhole(data);
  check(Update.written == 296, "last four bytes withheld before finish");
  check(pdks::ota::tailCount == 4, "four tail bytes retained");
  check(pdks::ota::received == SIZE, "all bytes received");
  check(Update.commitCount == 0, "no commit while tail is withheld");
  check(pdks::ota::transferHash.addCount == 2, "transfer hash fed per chunk");
  uploadEnd();
  check(pdks::ota::ended, "multipart end recorded");
  check(Update.commitCount == 0, "multipart end alone never commits");
  check(pdks::ota::busy(), "busy during upload");

  server.finalHandler();
  check(server.status == 200, "finish accepted");
  check(server.response ==
            "{\"ok\":true,\"message\":\"IMZALI FIRMWARE DOGRULANDI; CIHAZ YENIDEN BASLAYACAK\"}",
        "finish body");
  check(Update.written == SIZE, "tail written only at finish");
  check(Update.commitCount == 1, "exactly one commit");
  check(Update.abortCount == 0, "no abort");
  check(Update.endTrueCount == 0, "never end(true)");
  check(pdks::ota::reboot && pdks::ota::busy(), "reboot scheduled");
  check(pdks::ota::restartAt == testNow + 1500, "restart 1500ms after finish");
  check(!pdks::ota::prepared.ready, "prepare ticket consumed");

  pdks::ota::tick();
  check(ESP.restartCount == 0, "no restart before the deadline");
  testNow += 1499;
  pdks::ota::tick();
  check(ESP.restartCount == 0, "no restart one ms early");
  testNow += 1;
  pdks::ota::tick();
  check(ESP.restartCount == 1, "restart at the deadline");
}

static void testPrepareGuards() {
  // Every rejection below must happen before any updater work starts.
  resetAll();
  pdks::deviceauth::readyState = false;
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 403, "prepare rejects missing device credentials");
  check(server.response.find("KIMLIK/CSRF") != std::string::npos,
        "credential rejection message");
  check(!pdks::ota::prepared.ready, "no ticket without credentials");

  resetAll();
  server.authOk = false;
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 403, "prepare rejects bad http auth");

  resetAll();
  setHeaders(std::string(32, 'f'), "");
  callPrepare(validBody());
  check(server.status == 403, "prepare rejects wrong csrf value");

  resetAll();
  setHeaders(std::string(31, '0'), "");
  callPrepare(validBody());
  check(server.status == 403, "prepare rejects short csrf");

  resetAll();
  server.headers["X-CSRF-Token"] = "";
  callPrepare(validBody());
  check(server.status == 403, "prepare rejects missing csrf header");

  resetAll();
  pdks::ota::verifier.result = false;
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 403, "prepare rejects a bad publisher signature");
  check(server.response.find("YAYINCI") != std::string::npos,
        "publisher signature message");

  resetAll();
  setHeaders(CSRF, "");
  server.body = std::string(1025, 'x');
  server.prepareHandler();
  check(server.status == 413, "prepare rejects an oversized body");

  resetAll();
  setHeaders(CSRF, "");
  callPrepare(std::string("{not json"));
  check(server.status == 400, "prepare rejects invalid json");

  // Stale sequence: the offered build must be newer than the running one.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody(TARGET, "1.5.0", 10500, SIZE, SHA_ZERO, attendance.lastSequence(),
                          attendance.rosterVersion(), REVISION, "FIRMWARE GUNCELLE", SIGNATURE));
  check(server.status == 409, "prepare rejects a stale sequence");

  // Unknown target / mismatching version are refused by the envelope builder.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody("nodemcuv2", VERSION, SEQUENCE, SIZE, SHA_ZERO, 0,
                          attendance.rosterVersion(), REVISION, "FIRMWARE GUNCELLE", SIGNATURE));
  check(server.status == 400, "prepare rejects an unknown target");

  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody(TARGET, "1.5.2", SEQUENCE, SIZE, SHA_ZERO, 0,
                          attendance.rosterVersion(), REVISION, "FIRMWARE GUNCELLE", SIGNATURE));
  check(server.status == 400, "prepare rejects version/sequence mismatch");

  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody(TARGET, "1.6.0", SEQUENCE, SIZE, SHA_ZERO, 0,
                          attendance.rosterVersion(), REVISION, "FIRMWARE GUNCELLE", SIGNATURE));
  check(server.status == 400, "prepare rejects another version at this sequence");

  // Open session blocks an update.
  resetAll();
  check(attendance.upsertEmployee("00000001", "TEST") == pdks::Result::Ok, "employee added");
  pdks::Event entry;
  check(attendance.preview("00000001", 1704067200UL, entry) == pdks::Result::Ok,
        "entry preview");
  check(attendance.commit(entry) == pdks::Result::Ok, "entry committed");
  check(attendance.anyInside(), "employee is inside");
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 409, "prepare rejects an open session");

  // Mismatched state fields are refused.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody(TARGET, VERSION, SEQUENCE, SIZE, SHA_ZERO, 1,
                          attendance.rosterVersion(), REVISION, "FIRMWARE GUNCELLE", SIGNATURE));
  check(server.status == 409, "prepare rejects a wrong expected_sequence");

  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody(TARGET, VERSION, SEQUENCE, SIZE, SHA_ZERO, 0,
                          attendance.rosterVersion() ^ 1u, REVISION, "FIRMWARE GUNCELLE",
                          SIGNATURE));
  check(server.status == 409, "prepare rejects a wrong roster_version");

  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody(TARGET, VERSION, SEQUENCE, SIZE, SHA_ZERO, 0,
                          attendance.rosterVersion(), REVISION + 1, "FIRMWARE GUNCELLE",
                          SIGNATURE));
  check(server.status == 409, "prepare rejects a wrong journal_revision");

  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody(TARGET, VERSION, SEQUENCE, SIZE, SHA_ZERO, 0,
                          attendance.rosterVersion(), REVISION, "FIRMWARE GUNCELLE FOO",
                          SIGNATURE));
  check(server.status == 400, "prepare rejects a wrong confirm phrase");

  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody(TARGET, VERSION, SEQUENCE, SIZE, SHA_ZERO, 0,
                          attendance.rosterVersion(), REVISION, "FIRMWARE GUNCELLE",
                          std::string(342, 'A')));
  check(server.status == 400, "prepare rejects an unsigned-length signature");

  resetAll();
  pdks::storage::mountState = false;
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 409, "prepare rejects unmounted storage");

  resetAll();
  pdks::storage::lockState = true;
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 409, "prepare rejects locked storage");

  resetAll();
  pdks::storage::replayOk = false;
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 409, "prepare rejects an unverifiable journal");

  resetAll();
  ESP.freeSketchSpace = 4096;
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 409, "prepare rejects insufficient flash");

  resetAll();
  ESP.freeSketchSpace = 5000;  // free-4096 is 904, rounded down to 0
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(server.status == 409, "prepare rejects a firmware larger than free space");

  // Nothing above may have touched the updater.
  check(Update.beginCount == 0, "rejected prepares never begin the updater");
  check(Update.installCount == 1, "only setup installs the signature");
  check(!pdks::ota::busy(), "rejected prepares leave the server idle");
}

static void testUploadStartGuards() {
  resetAll();
  check(static_cast<bool>(server.uploadHandler), "upload handler present");

  // No ticket at all.
  setHeaders(CSRF, "");
  uploadStart("firmware", std::string(32, '0'));
  check(pdks::ota::error != nullptr, "upload without prepare is refused");
  check(Update.beginCount == 0, "no begin without prepare");

  // Wrong multipart field name.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(pdks::ota::prepared.ready, "ticket for field-name case");
  uploadStart("avatar", token());
  check(Update.beginCount == 0, "wrong field name refused");

  // Wrong or short token.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  uploadStart("firmware", std::string(32, '0'));
  check(Update.beginCount == 0, "wrong ota token refused");

  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  uploadStart("firmware", std::string(31, 'A'));
  check(Update.beginCount == 0, "short ota token refused");

  // CSRF no longer present at upload time.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  server.headers.erase("X-CSRF-Token");
  uploadStart("firmware", token());
  check(Update.beginCount == 0, "missing csrf at upload refused");

  // Health flag dropped between prepare and upload.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  storageHealthy = false;
  uploadStart("firmware", token());
  check(Update.beginCount == 0, "unhealthy storage at upload refused");

  // Expired ticket (60 s window).
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  testNow += 60000;
  uploadStart("firmware", token());
  check(Update.beginCount == 1 && Update.isRunning(), "upload at 60s boundary accepted");
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  testNow += 60001;
  uploadStart("firmware", token());
  check(Update.beginCount == 0, "expired ticket refused");

  check(Update.commitCount == 0, "start guards never commit");
  check(Update.endTrueCount == 0, "never end(true)");
}

static void testStateChangeBetweenPrepareAndUpload() {
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  check(pdks::ota::prepared.ready, "ticket ready before state change");
  pdks::storage::revisionState = REVISION + 1;
  uploadStart("firmware", token());
  check(Update.beginCount == 0, "state change between prepare and upload refused");
  check(!Update.isRunning(), "updater not started");
  check(!pdks::ota::active, "not active");
  check(pdks::ota::error != nullptr, "failure recorded");
  uploadEnd();
  server.finalHandler();
  check(server.status == 400, "finish reports the refused start");
  check(Update.commitCount == 0, "no commit after a refused start");
}

static void testFinishGuards() {
  // Auth lost only at the final handler must not commit the withheld tail.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  const std::vector<uint8_t> data = firmware();
  uploadStart("firmware", token());
  uploadWhole(data);
  uploadEnd();
  check(Update.written == 296, "tail withheld before final auth check");
  server.authOk = false;
  server.finalHandler();
  check(server.status == 409, "finish rejects lost auth");
  check(Update.commitCount == 0, "no accidental commit when final auth fails");
  check(Update.written == 296, "tail still not written");
  check(!Update.isRunning(), "updater aborted");
  check(!pdks::ota::active, "upload cleared");

  // Same, with the CSRF header removed at finish time.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  uploadStart("firmware", token());
  uploadWhole(data);
  uploadEnd();
  server.headers.erase("X-CSRF-Token");
  server.finalHandler();
  check(server.status == 409, "finish rejects a missing csrf header");
  check(Update.commitCount == 0, "no commit without csrf at finish");

  // Truncated upload.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  uploadStart("firmware", token());
  uploadChunk(data, 0, 200);
  uploadEnd();
  server.finalHandler();
  check(server.status == 409, "truncated upload rejected");
  check(Update.written == 196, "truncated upload wrote 196 bytes");
  check(Update.commitCount == 0, "truncated upload never commits");

  // Oversized chunk.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  uploadStart("firmware", token());
  uploadChunk(data, 0, 99);
  uploadChunk(data, 99, 202);
  check(pdks::ota::error != nullptr, "oversized chunk refused");
  server.finalHandler();
  check(server.status == 400, "oversized upload rejected");
  check(Update.written == 95, "oversized chunk never written");
  check(Update.commitCount == 0, "oversized upload never commits");

  // Aborted after the whole file arrived.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  uploadStart("firmware", token());
  uploadWhole(data);
  uploadAborted();
  check(pdks::ota::error != nullptr, "aborted transfer flagged");
  server.finalHandler();
  check(server.status == 400, "aborted upload rejected");
  check(Update.written == 296, "aborted upload never writes the tail");
  check(Update.commitCount == 0, "aborted upload never commits");

  // A second multipart file in the same request is refused before any commit.
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  uploadStart("firmware", token());
  uploadWhole(data);
  uploadEnd();
  check(Update.commitCount == 0, "first file not committed yet");
  uploadStart("firmware", token());
  check(pdks::ota::error != nullptr, "second file refused");
  check(!pdks::ota::active, "second file clears the active state");
  server.finalHandler();
  check(server.status == 400, "second file keeps the request failed");
  check(Update.commitCount == 0, "second file never commits");
}

static void testDigestGuard() {
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(prepareBody(TARGET, VERSION, SEQUENCE, SIZE, SHA_OTHER, attendance.lastSequence(),
                          attendance.rosterVersion(), REVISION, "FIRMWARE GUNCELLE", SIGNATURE));
  check(server.status == 200, "prepare accepts another digest envelope");
  check(std::string(pdks::ota::prepared.sha) == SHA_OTHER, "prepared keeps the offered digest");

  const std::vector<uint8_t> data = firmware();
  uploadStart("firmware", token());
  uploadWhole(data);
  uploadEnd();
  check(Update.written == 296, "tail withheld before the digest check");
  server.finalHandler();
  check(server.status == 400, "digest mismatch rejected");
  check(server.response.find("OZETI") != std::string::npos, "digest rejection message");
  check(Update.written == 296, "digest mismatch never writes the tail");
  check(Update.commitCount == 0, "digest mismatch never commits");
  check(Update.endTrueCount == 0, "never end(true)");
}

static void testPrepareWhileBusy() {
  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  uploadStart("firmware", token());
  server.body = validBody();
  server.prepareHandler();
  check(server.status == 409, "prepare refused while an upload runs");
  check(server.response.find("GUNCELLEME SURUYOR") != std::string::npos, "busy message");

  resetAll();
  setHeaders(CSRF, "");
  callPrepare(validBody());
  const std::vector<uint8_t> data = firmware();
  uploadStart("firmware", token());
  uploadWhole(data);
  uploadEnd();
  server.finalHandler();
  check(server.status == 200, "upload finished before busy check");
  server.body = validBody();
  server.prepareHandler();
  check(server.status == 409, "prepare refused while a restart is pending");
}

int main() {
  testValidUpload();
  testPrepareGuards();
  testUploadStartGuards();
  testStateChangeBetweenPrepareAndUpload();
  testFinishGuards();
  testDigestGuard();
  testPrepareWhileBusy();
  if (failures != 0) {
    std::printf("ota server: %d failure(s)\n", failures);
    return 1;
  }
  std::printf("ota server: all checks passed\n");
  return 0;
}
'''


def compiler():
    local = ROOT/'.platformio/packages/toolchain-gccmingw32/bin/g++.exe'
    if local.exists():
        return str(local)
    found = os.environ.get('CXX') or shutil.which('g++')
    if not found:
        raise SystemExit("Host C++ compiler required (CXX, MinGW toolchain or g++).")
    return found


def standard(tool, env):
    """The pinned PlatformIO MinGW g++ 5.1 only knows C++11."""
    try:
        version = subprocess.run([tool, '-dumpversion'], capture_output=True, text=True,
                                 env=env, check=True).stdout.strip()
        major = int(version.split('.')[0])
    except (OSError, ValueError, subprocess.CalledProcessError):
        major = 5
    return '-std=c++20' if major >= 10 else '-std=c++11'


def main():
    if not LIBRARY.exists():
        raise SystemExit("Install the project's pinned ArduinoJson dependency first.")
    tool = compiler()
    MOCKS.mkdir(parents=True, exist_ok=True)
    (MOCKS/'Arduino.h').write_text(ARDUINO_H, encoding='utf-8')
    (MOCKS/'ESP8266WebServer.h').write_text(WEBSERVER_H, encoding='utf-8')
    (MOCKS/'ESP8266WiFi.h').write_text(WIFI_H, encoding='utf-8')
    (MOCKS/'BearSSLHelpers.h').write_text(BEARSSL_H, encoding='utf-8')
    (MOCKS/'Updater.h').write_text(UPDATER_H, encoding='utf-8')
    (MOCKS/'pgmspace.h').write_text(PGMSPACE_H, encoding='utf-8')
    source = OUT/'ota_server_main.cpp'
    source.write_text(SOURCE, encoding='utf-8')

    env = dict(os.environ, PATH=str(Path(tool).resolve().parent)+os.pathsep+os.environ.get('PATH', ''))
    exe = OUT/('ota_server_tests.exe' if os.name == 'nt' else 'ota_server_tests')
    subprocess.run([tool, standard(tool, env), '-Wall', '-Wextra', '-Werror', '-pedantic',
                    '-DPDKS_ENABLE_OTA=1', '-DPDKS_ENABLE_SD=1', '-DPDKS_SD_SOFTSPI=1',
                    '-DPDKS_BUZZER_GPIO=1', '-DPDKS_UART_LOGGING=0',
                    '-I', str(MOCKS), '-I', str(ROOT/'include'), '-I', str(LIBRARY),
                    str(source), str(ROOT/'src/Attendance.cpp'), '-o', str(exe)],
                   cwd=ROOT, env=env, check=True)
    subprocess.run([str(exe)], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
