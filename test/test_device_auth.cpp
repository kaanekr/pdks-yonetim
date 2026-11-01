// Native tests for src/DeviceAuth.cpp: fail-closed loading, private USB
// bootstrap migration, and the public image refusing to create credentials.
// No device, no network, no real profile. Build/run: tools/test_device_auth.py
// compiles this file twice (private and public) into .pio/device-auth-tests.
#include <cstdio>
#include <cstring>
#include <map>
#include <string>

#include "DeviceAuth.h"
#include "LittleFS.h"
#include "ReleaseVersion.h"
#if !PDKS_PUBLIC_RELEASE
#include "Config.h"
#endif

namespace storemock {
std::map<std::string, std::string> files;
bool failWrite = false, failRead = false, failRename = false;
int formats = 0;
}  // namespace storemock
MockFS LittleFS;

namespace {
bool gMounted = true;
bool gLocked = false;
}  // namespace

namespace pdks {
namespace storage {
bool mounted() { return gMounted; }
bool locked() { return gLocked; }
}  // namespace storage
}  // namespace pdks

using namespace pdks;

static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; std::printf("FAIL %d: %s\n", __LINE__, #x); } } while (0)
#define CHECK_TEXT(expr, expected) CHECK(std::strcmp((expr), (expected)) == 0)

static const char* AUTH = "/device-auth.json";
static const char* TMP = "/device-auth.tmp";
static const char* ROSTER = "/employees.json";
static const char* JOURNAL = "/journal.log";

static const char* USER = "saha-operator";
static const char* PASS = "Saha-Sifre-2026";
static const char* SSID = "PDKS-Saha";
static const char* AP = "Kurulum-Sifre-9";

static std::string json(const std::string& user, const std::string& pass,
                        const std::string& ssid, const std::string& ap) {
  return "{\"user\":\"" + user + "\",\"password\":\"" + pass +
         "\",\"ap_ssid\":\"" + ssid + "\",\"ap_password\":\"" + ap + "\"}";
}

static bool has(const char* path) { return storemock::files.count(path) != 0; }

static std::string bodyOf(const char* path) {
  std::map<std::string, std::string>::const_iterator it =
      storemock::files.find(path);
  return it == storemock::files.end() ? std::string() : it->second;
}

static bool sentinels() {
  return bodyOf(ROSTER) == "roster-sentinel" &&
         bodyOf(JOURNAL) == "journal-sentinel" && !has(TMP) &&
         storemock::formats == 0;
}

static void reset() {
  storemock::files.clear();
  storemock::failWrite = storemock::failRead = storemock::failRename = false;
  storemock::formats = 0;
  gMounted = true;
  gLocked = false;
  storemock::files[ROSTER] = "roster-sentinel";
  storemock::files[JOURNAL] = "journal-sentinel";
}

static void checkUnready() {
  CHECK(!deviceauth::ready());
  CHECK_TEXT(deviceauth::username(), "");
  CHECK_TEXT(deviceauth::password(), "");
  CHECK_TEXT(deviceauth::apSsid(), "");
  CHECK_TEXT(deviceauth::apPassword(), "");
}

static void checkCredentials(const char* user, const char* pass,
                             const char* ssid, const char* ap) {
  CHECK(deviceauth::ready());
  CHECK_TEXT(deviceauth::username(), user);
  CHECK_TEXT(deviceauth::password(), pass);
  CHECK_TEXT(deviceauth::apSsid(), ssid);
  CHECK_TEXT(deviceauth::apPassword(), ap);
}

static void expectRejected(const char* label, const std::string& body) {
  reset();
  storemock::files[AUTH] = body;
  const bool started = deviceauth::begin();
  if (started) std::printf("  accepted invalid body: %s\n", label);
  CHECK(!started);
  checkUnready();
  CHECK(bodyOf(AUTH) == body); // never overwritten or repaired
  CHECK(sentinels());
}

static void testStorageGate() {
  reset();
  gMounted = false;
  CHECK(!deviceauth::begin());
  checkUnready();
  CHECK(!has(AUTH) && sentinels());

  gMounted = true;
  gLocked = true;
  CHECK(!deviceauth::begin());
  checkUnready();
  CHECK(!has(AUTH) && sentinels());
}

static void testExistingAccepted() {
  reset();
  storemock::files[AUTH] = json(USER, PASS, SSID, AP);
  CHECK(deviceauth::begin());
  checkCredentials(USER, PASS, SSID, AP);
  CHECK(bodyOf(AUTH) == json(USER, PASS, SSID, AP));
  CHECK(sentinels());
}

static void testReopenUnchanged() {
  reset();
  storemock::files[AUTH] = json(USER, PASS, SSID, AP);
  CHECK(deviceauth::begin());
  const std::string first = bodyOf(AUTH);
  CHECK(deviceauth::begin());
  checkCredentials(USER, PASS, SSID, AP);
  CHECK(bodyOf(AUTH) == first);
  CHECK(sentinels());
}

static void testRejects() {
  const std::string good = json(USER, PASS, SSID, AP);
  expectRejected("empty file", "");
  expectRejected("not json", "not json at all");
  expectRejected("short password", json(USER, "short7", SSID, AP));
  expectRejected("long password", json(USER, std::string(129, 'p'), SSID, AP));
  expectRejected("empty user", json("", PASS, SSID, AP));
  expectRejected("long user", json(std::string(49, 'u'), PASS, SSID, AP));
  expectRejected("user colon", json("op:erator", PASS, SSID, AP));
  expectRejected("empty ssid", json(USER, PASS, "", AP));
  expectRejected("long ssid", json(USER, PASS, std::string(33, 's'), AP));
  expectRejected("short ap password", json(USER, PASS, SSID, "short77"));
  expectRejected("long ap password", json(USER, PASS, SSID, std::string(64, 'a')));
  expectRejected("extra field",
                 "{\"user\":\"" + std::string(USER) + "\",\"password\":\"" +
                     std::string(PASS) + "\",\"ap_ssid\":\"" + std::string(SSID) +
                     "\",\"ap_password\":\"" + std::string(AP) + "\",\"v\":1}");
  expectRejected("missing field",
                 "{\"user\":\"" + std::string(USER) + "\",\"password\":\"" +
                     std::string(PASS) + "\",\"ap_ssid\":\"" + std::string(SSID) +
                     "\"}");
  expectRejected("numeric user",
                 "{\"user\":1,\"password\":\"" + std::string(PASS) +
                     "\",\"ap_ssid\":\"" + std::string(SSID) +
                     "\",\"ap_password\":\"" + std::string(AP) + "\"}");
  expectRejected("escaped NUL",
                 "{\"user\":\"" + std::string(USER) +
                     "\",\"password\":\"abcdefgh\\u0000z\",\"ap_ssid\":\"" +
                     std::string(SSID) + "\",\"ap_password\":\"" + std::string(AP) + "\"}");
  std::string control = "{\"user\":\"" + std::string(USER) +
                        "\",\"password\":\"abcdefg";
  control += static_cast<char>(1);
  control += "h\",\"ap_ssid\":\"" + std::string(SSID) +
             "\",\"ap_password\":\"" + std::string(AP) + "\"}";
  expectRejected("control char", control);
  std::string binary = good;
  binary.insert(binary.size() - 1, 1, '\0');
  expectRejected("raw NUL", binary);
  expectRejected("oversized file", good + std::string(1100, ' '));
}

static void testReadFailure() {
  reset();
  storemock::files[AUTH] = json(USER, PASS, SSID, AP);
  const std::string original = bodyOf(AUTH);
  storemock::failRead = true;
  CHECK(!deviceauth::begin());
  checkUnready();
  CHECK(bodyOf(AUTH) == original);
  CHECK(sentinels());
}

static void testReadyClearedOnFailure() {
  reset();
  storemock::files[AUTH] = json(USER, PASS, SSID, AP);
  CHECK(deviceauth::begin());
  checkCredentials(USER, PASS, SSID, AP);
  storemock::files[AUTH] = "broken";
  CHECK(!deviceauth::begin());
  checkUnready();
}

#if PDKS_PUBLIC_RELEASE
static void testPublicMissingWritesNothing() {
  reset();
  CHECK(!deviceauth::begin());
  checkUnready();
  CHECK(!has(AUTH) && !has(TMP));
  CHECK(sentinels());

  storemock::failWrite = true; // nothing should be attempted either way
  CHECK(!deviceauth::begin());
  checkUnready();
  CHECK(!has(AUTH) && !has(TMP) && sentinels());
}
#else
static void testPrivateBootstrap() {
  reset();
  CHECK(!has(AUTH));
  CHECK(deviceauth::begin());
  checkCredentials(cfg::ADMIN_USER, cfg::ADMIN_PASSWORD, cfg::AP_SSID,
                   cfg::AP_PASSWORD);
  CHECK(has(AUTH) && !has(TMP));
  const std::string first = bodyOf(AUTH);
  CHECK(first.size() <= 1024);
  CHECK(first == json(cfg::ADMIN_USER, cfg::ADMIN_PASSWORD, cfg::AP_SSID,
                      cfg::AP_PASSWORD));
  CHECK(sentinels());

  CHECK(deviceauth::begin()); // reopen reads the committed file unchanged
  checkCredentials(cfg::ADMIN_USER, cfg::ADMIN_PASSWORD, cfg::AP_SSID,
                   cfg::AP_PASSWORD);
  CHECK(bodyOf(AUTH) == first && sentinels());
}

static void testPrivateWriteFailure() {
  reset();
  storemock::failWrite = true;
  CHECK(!deviceauth::begin());
  checkUnready();
  CHECK(!has(AUTH) && !has(TMP));
  CHECK(sentinels());
  storemock::failWrite = false;
  CHECK(deviceauth::begin()); // a later clean attempt still migrates
  checkCredentials(cfg::ADMIN_USER, cfg::ADMIN_PASSWORD, cfg::AP_SSID,
                   cfg::AP_PASSWORD);
}

static void testPrivateRenameFailure() {
  reset();
  storemock::failRename = true;
  CHECK(!deviceauth::begin());
  checkUnready();
  CHECK(!has(AUTH) && !has(TMP));
  CHECK(sentinels());
}
#endif

int main() {
  testStorageGate();
  testExistingAccepted();
  testReopenUnchanged();
  testRejects();
  testReadFailure();
  testReadyClearedOnFailure();
#if PDKS_PUBLIC_RELEASE
  testPublicMissingWritesNothing();
#else
  testPrivateBootstrap();
  testPrivateWriteFailure();
  testPrivateRenameFailure();
#endif
  std::printf("device auth (%s build): %d checks, %d failures\n",
              PDKS_PUBLIC_RELEASE ? "public" : "private", checks, failures);
  return failures == 0 ? 0 : 1;
}
