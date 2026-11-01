#include "Storage.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if PDKS_ENABLE_SD
#if PDKS_SD_SOFTSPI
#include "SoftSdCard.h"
#else
#include <SD.h>
#include <SPI.h>
#endif
#endif

namespace pdks {
namespace storage {
namespace {

// Roster JSON needs a working pool for ~32 entries (values are copied into the
// pool when parsing from a Stream).
constexpr size_t JSON_POOL = 6144;
constexpr char VERSION_FIELD[] = "v";
constexpr uint8_t SCHEMA_VERSION = 2;

bool g_mounted = false;
bool g_locked = false;
bool g_ready = false;      // journal state below is in sync with the file
bool g_haveSeq = false;
uint32_t g_lastSeq = 0;
uint32_t g_lastEpoch = 0;
uint32_t g_lines = 0;

bool g_sdOk = false;
uint32_t g_revision = 0;
const char* g_rosterPath = EMPLOYEES_PATH;
const char* g_journalPath = JOURNAL_PATH;
constexpr const char* SELECTOR = "/attendance.current";
constexpr const char* SELECTOR_TMP = "/attendance.ptr.tmp";
constexpr const char* SD_RECEIPT = "/attendance.sd";
constexpr const char* SD_DIRTY = "/attendance.sd-dirty";
constexpr const char* ROSTER_SLOTS[] = {"/attendance-a.json", "/attendance-b.json"};
constexpr const char* JOURNAL_SLOTS[] = {"/attendance-a.log", "/attendance-b.log"};
bool g_pendingCleanup = false;
bool writeSmall(const char* path, const char* text);
bool removeKnown(const char* path);

void lockStorage() { g_locked = true; }

uint32_t crc32(const char* data, size_t len) {
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < len; ++i) {
    crc ^= (uint8_t)data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 1u) ? ((crc >> 1) ^ 0xEDB88320u) : (crc >> 1);
    }
  }
  return ~crc;
}

int hexValue(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

// Strict decimal, no sign, no leading zeros, no trailing garbage.
bool parseU32(const char* text, uint32_t& out) {
  if (!text || !*text) return false;
  if (text[0] == '0' && text[1] != '\0') return false;
  uint64_t value = 0;
  for (const char* p = text; *p; ++p) {
    if (*p < '0' || *p > '9') return false;
    value = value * 10u + (uint64_t)(*p - '0');
    if (value > 0xFFFFFFFFull) return false;
  }
  out = (uint32_t)value;
  return true;
}

int buildLine(char* out, size_t cap, const Event& ev) {
  char body[JOURNAL_MAX_LINE];
  int written = snprintf(body, sizeof(body), "%u,%lu,%lu,%s,%c,%lu",
                         ev.type == 'R' ? 2u : ev.type == 'M' ? 3u : 1u, (unsigned long)ev.sequence, (unsigned long)ev.epoch,
                         ev.uid, ev.type, (unsigned long)ev.seconds);
  if (written <= 0 || (size_t)written >= sizeof(body)) return -1;
  if (ev.name[0] || ev.archived || ev.nameRecorded) {
    if (ev.type == 'R') return -1;
    size_t length = 0;
    while (length < sizeof(ev.name) && ev.name[length]) ++length;
    if (!length || length >= sizeof(ev.name)) return -1;
    char encoded[97];
    const char* digits = "0123456789ABCDEF";
    for (size_t i = 0; i < length; ++i) {
      const unsigned char value = static_cast<unsigned char>(ev.name[i]);
      if (value < 32 || value == 127) return -1;
      encoded[2*i] = digits[value >> 4]; encoded[2*i+1] = digits[value & 15];
    }
    encoded[2*length] = '\0';
    written = snprintf(body, sizeof(body), "4,%lu,%lu,%s,%c,%lu,%s,%u,%u",
        (unsigned long)ev.sequence, (unsigned long)ev.epoch, ev.uid, ev.type,
        (unsigned long)ev.seconds, encoded, ev.nameRecorded ? 1u : 0u, ev.archived ? 1u : 0u);
    if (written <= 0 || (size_t)written >= sizeof(body)) return -1;
  }
  uint32_t crc = crc32(body, (size_t)written);
  int total = snprintf(out, cap, "%s,%08lX", body, (unsigned long)crc);
  if (total <= 0 || (size_t)total >= cap) return -1;
  return total;
}

int splitFields(char* line, char* fields[], int maxFields) {
  int count = 0;
  char* cursor = line;
  while (count < maxFields) {
    fields[count++] = cursor;
    char* comma = strchr(cursor, ',');
    if (!comma) break;
    *comma = '\0';
    cursor = comma + 1;
  }
  return count;
}

bool parseLine(char* line, size_t len, Event& ev, uint32_t& outSeq,
                uint32_t& outEpoch) {
  if (len < 5 || len >= JOURNAL_MAX_LINE) return false;
  if (memchr(line, '\0', len) != nullptr || line[len] != '\0') return false;

  char* lastComma = strrchr(line, ',');
  if (!lastComma) return false;
  const char* crcField = lastComma + 1;
  if (strlen(crcField) != 8) return false;

  uint32_t expectedCrc = 0;
  for (int i = 0; i < 8; ++i) {
    int nibble = hexValue(crcField[i]);
    if (nibble < 0) return false;
    expectedCrc = (expectedCrc << 4) | (uint32_t)nibble;
  }
  size_t bodyLen = (size_t)(lastComma - line);
  if (crc32(line, bodyLen) != expectedCrc) return false;

  *lastComma = '\0';
  char* fields[10];
  int count = splitFields(line, fields, 10);
  bool named = strcmp(fields[0], "4") == 0;
  if (count != (named ? 9 : 6)) return false;
  bool recovery = strcmp(fields[0], "2") == 0;
  bool manual = strcmp(fields[0], "3") == 0;
  if (!recovery && !manual && !named && strcmp(fields[0], "1") != 0) return false;

  uint32_t sequence = 0;
  uint32_t epoch = 0;
  uint32_t seconds = 0;
  if (!parseU32(fields[1], sequence) || sequence == 0) return false;
  if (!parseU32(fields[2], epoch)) return false;
  if (epoch < cfg::MIN_EPOCH || epoch > cfg::MAX_EPOCH) return false;

  size_t uidLen = strlen(fields[3]);
  if (uidLen == 0 || uidLen > 20) return false;
  char normalized[21];
  if (!Attendance::normalizeUid(fields[3], normalized)) return false;
  if (strcmp(normalized, fields[3]) != 0) return false;

  if (fields[4][0] == '\0' || fields[4][1] != '\0') return false;
  if (recovery) {
    if (fields[4][0] != 'R' || strcmp(fields[3], "00000000") != 0) return false;
  } else if (manual) {
    if (fields[4][0] != 'M') return false;
  } else if (fields[4][0] != 'I' && fields[4][0] != 'O' && !(named && fields[4][0] == 'M')) return false;
  if (!parseU32(fields[5], seconds)) return false;
  if ((manual || fields[4][0] == 'M') && (!seconds || seconds >= epoch || epoch-seconds < cfg::MIN_EPOCH)) return false;

  ev = Event();
  if (named) {
    const size_t length = strlen(fields[6]);
    if (!length || length > 96 || length % 2 ||
        (strcmp(fields[7], "0") && strcmp(fields[7], "1")) ||
        (strcmp(fields[8], "0") && strcmp(fields[8], "1"))) return false;
    bool visible = false;
    for (size_t i = 0; i < length/2; ++i) {
      int a = hexValue(fields[6][2*i]), b = hexValue(fields[6][2*i+1]);
      if (a < 0 || b < 0) return false;
      const unsigned char value = static_cast<unsigned char>((a << 4) | b);
      if (value < 32 || value == 127) return false;
      if (value != ' ') visible = true;
      ev.name[i] = static_cast<char>(value);
    }
    if (!visible) return false;
    ev.nameRecorded = fields[7][0] == '1';
    ev.archived = fields[8][0] == '1';
  }

  ev.sequence = sequence;
  ev.epoch = epoch;
  strncpy(ev.uid, fields[3], sizeof(ev.uid) - 1);
  ev.uid[sizeof(ev.uid) - 1] = '\0';
  ev.type = fields[4][0];
  ev.seconds = seconds;
  outSeq = sequence;
  outEpoch = epoch;
  return true;
}

bool validContinuation(const Event& event, bool have, uint32_t seq, uint32_t epoch) {
  if (seq == UINT32_MAX || event.sequence != (have ? seq + 1 : 1)) return false;
  if (event.type == 'R') return event.seconds == epoch;
  if (event.type == 'M') return event.seconds && event.seconds < event.epoch &&
                               event.epoch-event.seconds >= cfg::MIN_EPOCH;
  return !have || event.epoch >= epoch;
}

bool writeAndVerify(const char* line, size_t len) {
  File out = LittleFS.open(g_journalPath, "a");
  if (!out) return false;
  size_t written = out.write((const uint8_t*)line, len);
  bool ok = (written == len);
  if (ok) {
    ok = (out.write((const uint8_t*)"\n", 1) == 1);
  }
  out.flush();
  size_t newSize = out.size();
  out.close();
  if (!ok) return false;

  size_t need = len + 1;
  if (newSize < need) return false;

  File back = LittleFS.open(g_journalPath, "r");
  if (!back) return false;
  if (!back.seek((uint32_t)(newSize - need), SeekSet)) {
    back.close();
    return false;
  }
  char verify[JOURNAL_MAX_LINE];
  int got = back.read((uint8_t*)verify, need);
  back.close();
  if (got < 0 || (size_t)got != need) return false;
  if (verify[len] != '\n') return false;
  return memcmp(verify, line, len) == 0;
}

void mirrorToSd(const char* line, size_t len) {
#if PDKS_ENABLE_SD
  if (!g_sdOk) return;
#if PDKS_SD_SOFTSPI
  g_sdOk = softsd::appendLine(line, len);
#else
  File f = SD.open(SD_EVENTS_PATH, "a");
  if (!f) {
    g_sdOk = false;
    return;
  }
  size_t written = f.write((const uint8_t*)line, len);
  bool ok = (written == len);
  if (ok) ok = (f.write((const uint8_t*)"\n", 1) == 1);
  f.flush();
  f.close();
  if (!ok) g_sdOk = false;
#endif
#else
  (void)line;
  (void)len;
#endif
}

void appendJsonString(String& out, const char* text) {
  out += '"';
  for (const unsigned char* p = (const unsigned char*)text; *p; ++p) {
    unsigned char c = *p;
    if (c == '"' || c == '\\') {
      out += '\\';
      out += (char)c;
    } else if (c < 0x20) {
      out += ' ';
    } else {
      out += (char)c;
    }
  }
  out += '"';
}

void appendEntry(String& out, bool& first, const char* uid, const char* name,
                 const char* cardUid, bool active) {
  if (!first) out += ',';
  first = false;
  out += "{\"uid\":";
  appendJsonString(out, uid);
  out += ",\"name\":";
  appendJsonString(out, name);
  out += ",\"card_uid\":";
  appendJsonString(out, cardUid);
  out += active ? ",\"active\":true" : ",\"active\":false";
  out += '}';
}

bool validStoredName(const char* name) {
  if (!name || !*name) return false;
  size_t len = strlen(name);
  if (len > 48) return false;
  bool visible = false;
  for (const unsigned char* p = (const unsigned char*)name; *p; ++p) {
    if (*p < 0x20 || *p == 0x7F) return false;
    if (*p != ' ') visible = true;
  }
  return visible;
}

bool validStoredUid(const char* uid) {
  if (!uid || !*uid) return false;
  char normalized[21];
  if (!Attendance::normalizeUid(uid, normalized)) return false;
  return strcmp(normalized, uid) == 0;
}

bool readIdentity(JsonVariant entry, const char*& uid, const char*& name) {
  if (!entry.is<JsonObject>() || !entry["uid"].is<const char*>() ||
      !entry["name"].is<const char*>()) return false;
  JsonString u = entry["uid"].as<JsonString>();
  JsonString n = entry["name"].as<JsonString>();
  uid = u.c_str();
  name = n.c_str();
  if (!uid || !name || u.size() != strlen(uid) || n.size() != strlen(name))
    return false; // embedded JSON NUL must not silently truncate an identity
  return validStoredUid(uid) && validStoredName(name);
}

bool readManagement(JsonVariant entry, uint8_t version, const char* uid,
                    const char*& cardUid, bool& active) {
  cardUid = uid;
  active = true;
  if (version == 1) return true;
  if (!entry["card_uid"].is<const char*>() || !entry["active"].is<bool>()) return false;
  JsonString card = entry["card_uid"].as<JsonString>();
  cardUid = card.c_str();
  active = entry["active"].as<bool>();
  return cardUid && card.size() == strlen(cardUid) && validStoredUid(cardUid);
}

Status verifyEmployeesFile(size_t expectedCount, const Attendance& core,
                           const Employee* candidate) {
  File f = LittleFS.open(EMPLOYEES_TMP_PATH, "r");
  if (!f) return Status::IoError;
  if (f.size() == 0 || f.size() > EMPLOYEES_JSON_MAX) {
    f.close();
    return Status::IntegrityError;
  }
  DynamicJsonDocument doc(JSON_POOL);
  DeserializationError err =
      deserializeJson(doc, f, DeserializationOption::NestingLimit(3));
  f.close();
  if (err) return Status::IntegrityError;

  if (!doc["v"].is<uint8_t>() || doc["v"].as<uint8_t>() != SCHEMA_VERSION) {
    return Status::IntegrityError;
  }
  JsonArray list = doc["employees"].as<JsonArray>();
  if (list.isNull() || list.size() != expectedCount) return Status::IntegrityError;

  const char* seen[MAX_EMPLOYEES] = {};
  size_t seenCount = 0;
  for (JsonVariant entry : list) {
    const char* uid = nullptr;
    const char* name = nullptr;
    if (!readIdentity(entry, uid, name)) {
      return Status::IntegrityError;
    }
    if (seenCount >= MAX_EMPLOYEES) return Status::IntegrityError;
    for (size_t i = 0; i < seenCount; ++i) {
      if (strcmp(seen[i], uid) == 0) return Status::IntegrityError;
    }
    seen[seenCount++] = uid;
    const Employee* existing = core.find(uid);
    const char* expectedName = nullptr;
    if (existing) {
      expectedName = (candidate && strcmp(candidate->uid, uid) == 0)
                         ? candidate->name
                         : existing->name;
    } else if (candidate && strcmp(candidate->uid, uid) == 0) {
      expectedName = candidate->name;
    } else {
      return Status::IntegrityError;
    }
    const char* cardUid = nullptr;
    bool active = true;
    if (!readManagement(entry, SCHEMA_VERSION, uid, cardUid, active)) return Status::IntegrityError;
    const char* expectedCard = existing ? existing->cardUid : uid;
    bool expectedActive = existing ? existing->active : true;
    if (strcmp(expectedName, name) != 0 || strcmp(expectedCard, cardUid) != 0 ||
        active != expectedActive) return Status::IntegrityError;
  }
  return Status::Ok;
}

} // namespace

const char* statusText(Status status) {
  switch (status) {
    case Status::Ok: return "OK";
    case Status::NotMounted: return "FS YOK";
    case Status::Locked: return "KILITLI";
    case Status::IoError: return "IO HATASI";
    case Status::IntegrityError: return "BOZUK KAYIT";
    case Status::NoSpace: return "YER YOK";
    case Status::BadData: return "GECERSIZ";
  }
  return "?";
}

bool mount() {
  if (g_mounted) return true;
  LittleFSConfig fsConfig;
  fsConfig.setAutoFormat(false); // never auto-format, not even on mount failure
  if (!LittleFS.setConfig(fsConfig)) return false;
  g_mounted = LittleFS.begin();
  if (g_mounted) {
    g_revision = 0; g_rosterPath = EMPLOYEES_PATH; g_journalPath = JOURNAL_PATH;
    g_pendingCleanup = false;
    if (LittleFS.exists(SELECTOR)) {
      File file = LittleFS.open(SELECTOR, "r");
      char text[64] = {};
      const size_t size = file ? file.size() : 0;
      const int got = size && size < sizeof(text) ? file.read(reinterpret_cast<uint8_t*>(text), size) : -1;
      file.close();
      char* comma = strrchr(text, ',');
      uint32_t revision = 0, checksum = 0;
      bool valid = got >= 0 && static_cast<size_t>(got) == size && comma && strlen(comma+1) == 8;
      if (valid) {
        for (int i=0; i<8; ++i) {
          int digit = hexValue(comma[1+i]);
          if (digit < 0) { valid = false; break; }
          checksum = (checksum << 4) | static_cast<uint32_t>(digit);
        }
        valid = valid && crc32(text, static_cast<size_t>(comma-text)) == checksum;
        *comma = '\0';
        valid = valid && parseU32(text, revision) && revision > 0;
      }
      if (!valid) { lockStorage(); return false; }
      g_revision = revision;
      const unsigned slot = revision & 1u;
      g_rosterPath = ROSTER_SLOTS[slot]; g_journalPath = JOURNAL_SLOTS[slot];
      if (!LittleFS.exists(g_rosterPath) || !LittleFS.exists(g_journalPath)) { lockStorage(); return false; }
      g_pendingCleanup = true;
    }
  }
  return g_mounted;
}

bool mounted() { return g_mounted; }
bool locked() { return g_locked; }

Snapshot snapshot() {
  Snapshot snap;
  snap.mounted = g_mounted;
  snap.locked = g_locked;
  snap.journalLines = g_lines;
  if (!g_mounted) return snap;

  snap.employeesPresent = LittleFS.exists(g_rosterPath);
  snap.journalPresent = LittleFS.exists(g_journalPath);
  if (snap.employeesPresent) {
    File f = LittleFS.open(g_rosterPath, "r");
    if (f) {
      snap.employeesBytes = (uint32_t)f.size();
      f.close();
    }
  }
  if (snap.journalPresent) {
    File f = LittleFS.open(g_journalPath, "r");
    if (f) {
      snap.journalBytes = (uint32_t)f.size();
      f.close();
    }
  }
  FSInfo info;
  if (LittleFS.info(info)) {
    snap.freeBytes = (info.totalBytes > info.usedBytes)
                         ? (uint32_t)(info.totalBytes - info.usedBytes)
                         : 0;
  }
  return snap;
}

Status operatorFormat() {
  Snapshot snap = snapshot();
  bool blank = !snap.employeesPresent && !snap.journalPresent;
  if (LittleFS.exists(SELECTOR) || (g_mounted && !blank)) return Status::BadData;

  LittleFSConfig fsConfig;
  fsConfig.setAutoFormat(false);
  LittleFS.setConfig(fsConfig); // ignored when already mounted

  if (!LittleFS.format()) {
    g_mounted = false;
    return Status::IoError;
  }
  g_mounted = LittleFS.begin();
  if (!g_mounted) return Status::IoError;

  g_locked = false;
  g_ready = true;
  g_haveSeq = false;
  g_lastSeq = 0;
  g_lastEpoch = 0;
  g_lines = 0;
  return Status::Ok;
}

Status appendEvent(const Event& event) {
  if (!g_mounted) return Status::NotMounted;
  if (g_locked) return Status::Locked;
  if (!g_ready) return Status::BadData;
  if (event.archived) return Status::BadData; // archived flags only originate in a verified rewrite
  if (event.type != 'I' && event.type != 'O' && event.type != 'R' && event.type != 'M') return Status::BadData;
  if (!validStoredUid(event.uid)) return Status::BadData;
  if (event.type == 'R' && strcmp(event.uid, "00000000") != 0) return Status::BadData;

  if (!validContinuation(event, g_haveSeq, g_lastSeq, g_lastEpoch)) return Status::BadData;
  if (event.epoch < cfg::MIN_EPOCH || event.epoch > cfg::MAX_EPOCH) {
    return Status::BadData;
  }

  char line[JOURNAL_MAX_LINE];
  int length = buildLine(line, sizeof(line), event);
  if (length <= 0 || (size_t)length + 2 > sizeof(line)) return Status::BadData;

  Snapshot snap = snapshot();
  if (snap.freeBytes < MIN_FREE_BYTES) {
    lockStorage(); // no silent rotation, no destructive recovery
    return Status::NoSpace;
  }

  // A durable dirty bit precedes the canonical append. If SD append/power fails,
  // a later boot must rebuild the mirror rather than advertise a silent gap.
  if (cfg::ENABLE_SD && !writeSmall(SD_DIRTY, "1")) {
    lockStorage(); return Status::IoError;
  }
  if (!writeAndVerify(line, (size_t)length)) {
    lockStorage();
    return Status::IoError;
  }

  g_lastSeq = event.sequence;
  g_haveSeq = true;
  if (event.type != 'M' || event.epoch > g_lastEpoch) g_lastEpoch = event.epoch;
  ++g_lines;
  mirrorToSd(line, (size_t)length); // SD is a mirror only; failure is a warning
  if (cfg::ENABLE_SD && g_sdOk && !removeKnown(SD_DIRTY)) g_sdOk = false;
  return Status::Ok;
}

Status loadEmployees(Attendance& core) {
  if (!g_mounted) return Status::NotMounted;
  if (g_locked) return Status::Locked;
  if (!LittleFS.exists(g_rosterPath)) return Status::Ok; // empty roster

  File f = LittleFS.open(g_rosterPath, "r");
  if (!f) return Status::IoError;
  size_t size = f.size();
  if (size == 0 || size > EMPLOYEES_JSON_MAX) {
    f.close();
    lockStorage();
    return Status::IntegrityError;
  }

  DynamicJsonDocument doc(JSON_POOL);
  DeserializationError err =
      deserializeJson(doc, f, DeserializationOption::NestingLimit(3));
  f.close();
  if (err) {
    lockStorage();
    return Status::IntegrityError;
  }
  if (!doc["v"].is<uint8_t>() || (doc["v"].as<uint8_t>() != 1 && doc["v"].as<uint8_t>() != SCHEMA_VERSION)) {
    lockStorage();
    return Status::IntegrityError;
  }
  JsonArray list = doc["employees"].as<JsonArray>();
  if (list.isNull() || list.size() > MAX_EMPLOYEES) {
    lockStorage();
    return Status::IntegrityError;
  }

  // A failure may leave a partial roster: the caller must keep scans disabled.
  for (JsonVariant entry : list) {
    const char* uid = nullptr;
    const char* name = nullptr;
    if (!readIdentity(entry, uid, name)) {
      lockStorage();
      return Status::IntegrityError;
    }
    const char* cardUid = nullptr;
    bool active = true;
    if (!readManagement(entry, doc["v"].as<uint8_t>(), uid, cardUid, active)) {
      lockStorage();
      return Status::IntegrityError;
    }
    if (core.find(uid)) { // duplicate uid
      lockStorage();
      return Status::IntegrityError;
    }
    if (core.count() >= MAX_EMPLOYEES) {
      lockStorage();
      return Status::IntegrityError;
    }
    if (core.upsertEmployee(uid, name) != Result::Ok) {
      lockStorage();
      return Status::IntegrityError;
    }
    if (core.updateEmployee(uid, name, cardUid, active) != Result::Ok) {
      lockStorage();
      return Status::IntegrityError;
    }
  }
  return Status::Ok;
}

Status saveEmployees(const Attendance& core, const Employee* candidate) {
  if (!g_mounted) return Status::NotMounted;
  if (g_locked) return Status::Locked;

  if (candidate) {
    if (!validStoredUid(candidate->uid) || !validStoredName(candidate->name)) {
      return Status::BadData;
    }
  }

  String out;
  out.reserve(256 + core.count() * 80);
  out += "{\"v\":2,\"employees\":[";
  bool first = true;
  size_t expectedCount = 0;
  for (size_t i = 0; i < core.count(); ++i) {
    const Employee* employee = core.at(i);
    if (!employee) break;
    const char* name =
        (candidate && strcmp(candidate->uid, employee->uid) == 0)
            ? candidate->name
            : employee->name;
    appendEntry(out, first, employee->uid, name, employee->cardUid, employee->active);
    ++expectedCount;
  }
  if (candidate && !core.find(candidate->uid)) {
    appendEntry(out, first, candidate->uid, candidate->name, candidate->uid, true);
    ++expectedCount;
  }
  out += "]}";

  if (expectedCount > MAX_EMPLOYEES) return Status::BadData;
  if (out.length() > EMPLOYEES_JSON_MAX) return Status::BadData;

  File f = LittleFS.open(EMPLOYEES_TMP_PATH, "w");
  if (!f) return Status::IoError;
  size_t written = f.write((const uint8_t*)out.c_str(), out.length());
  f.flush();
  f.close();
  if (written != out.length()) {
    LittleFS.remove(EMPLOYEES_TMP_PATH);
    return Status::IoError;
  }

  Status verify = verifyEmployeesFile(expectedCount, core, candidate);
  if (verify != Status::Ok) {
    LittleFS.remove(EMPLOYEES_TMP_PATH);
    return verify;
  }

  if (!LittleFS.rename(EMPLOYEES_TMP_PATH, g_rosterPath)) {
    LittleFS.remove(EMPLOYEES_TMP_PATH);
    return Status::IoError;
  }
  return Status::Ok;
}

Status replayJournal(EventVisitor visitor, void* ctx) {
  if (!g_mounted) return Status::NotMounted;
  if (g_locked) return Status::Locked;

  bool haveSeq = false;
  uint32_t seq = 0;
  uint32_t lastEpoch = 0;
  uint32_t lines = 0;

  if (!LittleFS.exists(g_journalPath)) {
    g_haveSeq = false;
    g_lastSeq = 0;
    g_lastEpoch = 0;
    g_lines = 0;
    g_ready = true;
    return Status::Ok;
  }

  File f = LittleFS.open(g_journalPath, "r");
  if (!f) {
    lockStorage();
    return Status::IoError;
  }

  char line[JOURNAL_MAX_LINE];
  size_t used = 0;
  int raw = 0;
  while ((raw = f.read()) >= 0) {
    char c = (char)raw;
    if (c == '\n') {
      if (used == 0) { // empty line inside the log is corruption
        f.close();
        lockStorage();
        return Status::IntegrityError;
      }
      line[used] = '\0';
      Event event;
      uint32_t parsedSeq = 0;
      uint32_t parsedEpoch = 0;
      if (!parseLine(line, used, event, parsedSeq, parsedEpoch)) {
        f.close();
        lockStorage();
        return Status::IntegrityError;
      }
      if (!validContinuation(event, haveSeq, seq, lastEpoch)) {
        f.close();
        lockStorage();
        return Status::IntegrityError;
      }
      if (visitor && !visitor(event, ctx)) {
        f.close();
        lockStorage();
        return Status::IntegrityError;
      }
      seq = parsedSeq;
      if (event.type != 'M' || parsedEpoch > lastEpoch) lastEpoch = parsedEpoch;
      haveSeq = true;
      ++lines;
      used = 0;
      if ((lines & 0x1F) == 0) yield();
    } else {
      if (c == '\r') { // strict: the writer never emits CR
        f.close();
        lockStorage();
        return Status::IntegrityError;
      }
      if (used >= JOURNAL_MAX_LINE - 2) {
        f.close();
        lockStorage();
        return Status::IntegrityError;
      }
      line[used++] = c;
    }
  }
  bool readComplete = f.position() == f.size();
  f.close();

  if (!readComplete || used != 0) { // I/O failure or partial trailing line
    lockStorage();
    return Status::IntegrityError;
  }

  g_haveSeq = haveSeq;
  g_lastSeq = seq;
  g_lastEpoch = lastEpoch;
  g_lines = lines;
  g_ready = true;
  return Status::Ok;
}

namespace {
struct ManualRange {
  const char* uid;
  uint32_t start,end;
  bool overlap;
};
bool visitManualRange(const Event& event, void* raw) {
  ManualRange& range = *static_cast<ManualRange*>(raw);
  if ((event.type=='O' || event.type=='M') && event.seconds && event.seconds<event.epoch &&
      strcmp(event.uid,range.uid)==0 && range.start<event.epoch &&
      event.epoch-event.seconds<range.end) range.overlap=true;
  return true; // always finish integrity validation, even after a match
}
}

Status manualOverlap(const char* uid, uint32_t start, uint32_t end, bool& overlap) {
  overlap = false;
  if (!validStoredUid(uid) || start<cfg::MIN_EPOCH || end<=start || end>cfg::MAX_EPOCH)
    return Status::BadData;
  ManualRange range = {uid,start,end,false};
  Status result = replayJournal(visitManualRange,&range);
  if (result==Status::Ok) overlap=range.overlap;
  return result;
}

namespace {
struct FindEvent { uint32_t sequence; Event event; bool found; };
bool visitFind(const Event& event, void* raw) {
  FindEvent& find = *static_cast<FindEvent*>(raw);
  if (event.sequence == find.sequence) { find.event = event; find.found = true; }
  return true;
}
struct History { const char* uid; bool found; };
bool visitHistory(const Event& event, void* raw) {
  History& find = *static_cast<History*>(raw);
  if (event.type != 'R' && strcmp(event.uid, find.uid) == 0) find.found = true;
  return true;
}
struct PairPlan {
  const Event* selected;
  uint32_t open = 0, epoch = 0, first = 0, second = 0;
  bool invalid = false;
};
bool visitPair(const Event& event, void* raw) {
  PairPlan& plan = *static_cast<PairPlan*>(raw);
  if (event.type == 'R') { plan.open = 0; return true; }
  if (strcmp(event.uid, plan.selected->uid) != 0) return true;
  if (event.type == 'I') {
    if (plan.open) plan.invalid = true;
    plan.open = event.sequence; plan.epoch = event.epoch;
  } else if (event.type == 'O') {
    if (event.sequence == plan.selected->sequence || plan.open == plan.selected->sequence) {
      if (!plan.open || event.epoch < plan.epoch || event.epoch-plan.epoch != event.seconds)
        plan.invalid = true;
      plan.first = plan.open; plan.second = event.sequence;
    }
    plan.open = 0;
  }
  return true;
}
bool removeKnown(const char* path) {
  return !LittleFS.exists(path) || (LittleFS.remove(path) && !LittleFS.exists(path));
}
bool smallMatches(const char* path, const char* text) {
  File file = LittleFS.open(path, "r");
  const size_t length = strlen(text);
  char buffer[64] = {};
  bool ok = file && length < sizeof(buffer) && file.size() == length &&
      file.read(reinterpret_cast<uint8_t*>(buffer), length) == static_cast<int>(length) &&
      memcmp(buffer, text, length) == 0;
  file.close();
  return ok;
}
bool writeSmall(const char* path, const char* text) {
  File file = LittleFS.open(path, "w");
  if (!file) return false;
  const size_t length = strlen(text);
  bool ok = file.write(reinterpret_cast<const uint8_t*>(text), length) == length;
  file.flush(); file.close();
  return ok && smallMatches(path, text);
}
bool copyIdentities(const Attendance& core, Attendance& next, const char* omitted) {
  for (size_t i=0; i<core.count(); ++i) {
    const Employee* employee = core.at(i);
    if (omitted && strcmp(omitted, employee->uid) == 0) continue;
    if (next.addEmployee(employee->uid, employee->name, employee->cardUid, employee->active) != Result::Ok)
      return false;
  }
  return true;
}
struct Rewrite {
  const Attendance* original;
  Attendance* next;
  File* file;
  const char* omitted;
  uint32_t first, second, sequence = 0, baseline = 0;
  bool ok = true;
};
bool visitRewrite(const Event& event, void* raw) {
  Rewrite& rewrite = *static_cast<Rewrite*>(raw);
  if (!rewrite.ok || event.sequence == rewrite.first || event.sequence == rewrite.second) return true;
  Event next = event;
  next.sequence = ++rewrite.sequence;
  if (next.type == 'R') next.seconds = rewrite.baseline;
  else {
    if (!next.name[0]) {
      const Employee* employee = rewrite.original->find(next.uid);
      if (!employee) { rewrite.ok = false; return true; }
      memcpy(next.name, employee->name, sizeof(next.name));
      next.nameRecorded = false; // legacy name inferred at explicit migration time
    }
    if (rewrite.omitted && strcmp(next.uid, rewrite.omitted) == 0) next.archived = true;
  }
  if (next.type != 'M' || next.epoch > rewrite.baseline) rewrite.baseline = next.epoch;
  char line[JOURNAL_MAX_LINE];
  int length = buildLine(line, sizeof(line), next);
  if (rewrite.next->commit(next) != Result::Ok || length <= 0 ||
      rewrite.file->write(reinterpret_cast<const uint8_t*>(line), static_cast<size_t>(length)) != static_cast<size_t>(length) ||
      rewrite.file->write(reinterpret_cast<const uint8_t*>("\n"), 1) != 1) rewrite.ok = false;
  return true;
}
bool replayCandidate(const Event& event, void* raw) {
  return static_cast<Attendance*>(raw)->commit(event) == Result::Ok;
}
Status rewriteGeneration(Attendance& core, const char* omitted, uint32_t first, uint32_t second) {
  if (!g_mounted || g_locked || !g_ready || g_revision == UINT32_MAX) return Status::Locked;
  if (cfg::ENABLE_SD && !g_sdOk) return Status::BadData;
  const uint32_t revision = g_revision+1;
  const unsigned slot = revision & 1u;
  // Reclaim only an uncommitted inactive slot before measuring headroom. A
  // pre-selector power loss must not strand its temporary full-log copy forever.
  if (!removeKnown(ROSTER_SLOTS[slot]) || !removeKnown(JOURNAL_SLOTS[slot])) return Status::IoError;
  Snapshot current = snapshot();
  if (current.freeBytes < current.journalBytes + current.employeesBytes + MIN_FREE_BYTES*2u)
    return Status::NoSpace;
  Attendance* candidate = new Attendance();
  if (!candidate) return Status::IoError;
  if (!copyIdentities(core, *candidate, omitted)) { delete candidate; return Status::BadData; }
  const char* oldRoster = g_rosterPath;
  const char* oldJournal = g_journalPath;
  g_rosterPath = ROSTER_SLOTS[slot];
  Status saved = saveEmployees(*candidate, nullptr);
  g_rosterPath = oldRoster;
  if (saved != Status::Ok) { delete candidate; return saved; }
  File file = LittleFS.open(JOURNAL_SLOTS[slot], "w");
  if (!file) { delete candidate; return Status::IoError; }
  Rewrite rewrite;
  rewrite.original=&core; rewrite.next=candidate; rewrite.file=&file;
  rewrite.omitted=omitted; rewrite.first=first; rewrite.second=second;
  Status walked = replayJournal(visitRewrite, &rewrite);
  file.flush(); file.close();
  if (walked != Status::Ok || !rewrite.ok) { delete candidate; return Status::IntegrityError; }
  // Verify the complete read-back with a fresh core before committing either file.
  candidate->clear(); // avoid a ~4 KiB temporary on the ESP8266's small stack
  g_rosterPath = ROSTER_SLOTS[slot]; g_journalPath = JOURNAL_SLOTS[slot];
  saved = loadEmployees(*candidate);
  if (saved == Status::Ok) saved = replayJournal(replayCandidate, candidate);
  g_rosterPath = oldRoster; g_journalPath = oldJournal;
  if (saved != Status::Ok) { delete candidate; return saved; }
  char body[16], selector[32];
  snprintf(body, sizeof(body), "%lu", (unsigned long)revision);
  snprintf(selector, sizeof(selector), "%s,%08lX", body, (unsigned long)crc32(body, strlen(body)));
  if (!writeSmall(SELECTOR_TMP, selector) || !LittleFS.rename(SELECTOR_TMP, SELECTOR)) {
    // The canonical generation did not change. Restore its append bookkeeping.
    replayJournal(nullptr, nullptr);
    delete candidate; return Status::IoError;
  }
  g_revision = revision; g_rosterPath = ROSTER_SLOTS[slot]; g_journalPath = JOURNAL_SLOTS[slot];
  g_pendingCleanup = true;
  core = *candidate;
  delete candidate;
  return completePendingRewrite();
}
bool visitMirror(const Event& event, void*) {
  char line[JOURNAL_MAX_LINE];
  int length = buildLine(line, sizeof(line), event);
  if (length <= 0) g_sdOk = false;
  else mirrorToSd(line, static_cast<size_t>(length));
  return true;
}
bool rebuildMirror() {
  bool reset = false;
#if PDKS_ENABLE_SD
#if PDKS_SD_SOFTSPI
  reset = g_sdOk && softsd::resetEvents();
#else
  reset = g_sdOk && (!SD.exists(SD_EVENTS_PATH) || SD.remove(SD_EVENTS_PATH));
#endif
#endif
  if (!reset) { g_sdOk = false; return false; }
  Status mirrored = replayJournal(visitMirror, nullptr);
  if (mirrored != Status::Ok || !g_sdOk) { g_sdOk = false; return false; }
  return true;
}
} // namespace

uint32_t journalRevision() { return g_revision; }

Status uidHasHistory(const char* uid, bool& found) {
  found = false;
  if (!validStoredUid(uid)) return Status::BadData;
  History history = {uid, false};
  Status status = replayJournal(visitHistory, &history);
  if (status == Status::Ok) found = history.found;
  return status;
}

Status deleteEmployee(Attendance& core, const char* uid) {
  const Employee* employee = core.find(uid);
  if (!employee || !validStoredUid(uid) || employee->inside) return Status::BadData;
  return rewriteGeneration(core, uid, 0, 0);
}

Status deleteSession(Attendance& core, uint32_t targetSequence) {
  FindEvent find;
  find.sequence=targetSequence; find.found=false;
  Status status = replayJournal(visitFind, &find);
  if (status != Status::Ok) return status;
  if (!find.found || find.event.type == 'R') return Status::BadData;
  if (find.event.type == 'M') return rewriteGeneration(core, nullptr, targetSequence, 0);
  PairPlan pair;
  pair.selected = &find.event;
  status = replayJournal(visitPair, &pair);
  if (status != Status::Ok) return status;
  if (pair.invalid || !pair.first || !pair.second) return Status::BadData;
  return rewriteGeneration(core, nullptr, pair.first, pair.second);
}

Status completePendingRewrite() {
  if (!g_pendingCleanup) return Status::Ok;
  if (!g_ready || g_locked) return Status::Locked;
  char revision[16];
  snprintf(revision, sizeof(revision), "%lu", (unsigned long)g_revision);
  if (cfg::ENABLE_SD && (!smallMatches(SD_RECEIPT, revision) || LittleFS.exists(SD_DIRTY))) {
    if (!rebuildMirror() || !writeSmall(SD_RECEIPT, revision) || !removeKnown(SD_DIRTY)) {
      lockStorage(); return Status::IoError;
    }
  }
  const unsigned inactive = (g_revision & 1u) ^ 1u;
  const char* old[] = {ROSTER_SLOTS[inactive], JOURNAL_SLOTS[inactive], EMPLOYEES_PATH,
                      JOURNAL_PATH, EMPLOYEES_TMP_PATH, SELECTOR_TMP};
  for (const char* path : old) {
    if (!removeKnown(path)) { lockStorage(); return Status::IoError; }
  }
  g_pendingCleanup = false;
  return Status::Ok;
}

bool sdEnabled() { return cfg::ENABLE_SD; }
bool sdOk() { return g_sdOk; }

bool beginSd() {
#if PDKS_ENABLE_SD
  digitalWrite(cfg::PIN_SD_CS, HIGH);
  pinMode(cfg::PIN_SD_CS, OUTPUT);
#if PDKS_SD_SOFTSPI
  g_sdOk = softsd::begin();
#else
  g_sdOk = SD.begin(cfg::PIN_SD_CS);
#endif
  if (g_sdOk && g_pendingCleanup && completePendingRewrite() != Status::Ok) g_sdOk = false;
  if (g_sdOk && !g_pendingCleanup && LittleFS.exists(SD_DIRTY)) {
    if (!g_ready || g_locked || !rebuildMirror() || !removeKnown(SD_DIRTY)) {
      g_sdOk = false; lockStorage();
    }
  }
  if (!g_sdOk && g_pendingCleanup) lockStorage();
  return g_sdOk;
#else
  return false;
#endif
}

} // namespace storage
} // namespace pdks
