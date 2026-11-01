// Native executable tests for pdks::Attendance. No Arduino dependencies.
// Build (host): g++ -std=c++11 -Iinclude src/Attendance.cpp test/test_attendance.cpp -o attendance_tests

#include "Attendance.h"

#include <cstdint>
#include <cstdio>
#include <cstring>

using namespace pdks;

static int g_checks = 0;
static int g_failures = 0;

static void reportFailure(const char* expression, const char* file, int line) {
  ++g_failures;
  std::printf("FAIL %s:%d: %s\n", file, line, expression);
}

// Custom CHECK: independent of NDEBUG, unlike assert().
#define CHECK(expression)                                      \
  do {                                                         \
    ++g_checks;                                                \
    if (!(expression)) {                                       \
      reportFailure(#expression, __FILE__, __LINE__);          \
    }                                                          \
  } while (0)

#define CHECK_RESULT(expression, expected) CHECK((expression) == (expected))

struct Snapshot {
  size_t count = 0;
  uint32_t sequence = 0;
  uint32_t lastEpoch = 0;
  bool inside = false;
  uint32_t entryEpoch = 0;
  uint32_t lastScanEpoch = 0;
  uint64_t totalSeconds = 0;
  char name[49] = {};
};

static Snapshot take(const Attendance& attendance, const char* uid) {
  Snapshot snapshot;
  snapshot.count = attendance.count();
  snapshot.sequence = attendance.lastSequence();
  snapshot.lastEpoch = attendance.lastEpoch();
  const Employee* employee = attendance.find(uid);
  if (employee != nullptr) {
    snapshot.inside = employee->inside;
    snapshot.entryEpoch = employee->entryEpoch;
    snapshot.lastScanEpoch = employee->lastScanEpoch;
    snapshot.totalSeconds = employee->totalSeconds;
    memcpy(snapshot.name, employee->name, sizeof(snapshot.name));
  }
  return snapshot;
}

static bool same(const Snapshot& left, const Snapshot& right) {
  return left.count == right.count && left.sequence == right.sequence &&
         left.lastEpoch == right.lastEpoch && left.inside == right.inside &&
         left.entryEpoch == right.entryEpoch &&
         left.lastScanEpoch == right.lastScanEpoch &&
         left.totalSeconds == right.totalSeconds &&
         strcmp(left.name, right.name) == 0;
}

static void sentinel(Event& event) {
  event.sequence = 77777;
  event.epoch = 99999;
  memcpy(event.uid, "DEADBEEF", 9);
  event.type = 'X';
  event.seconds = 4242;
}

static bool isSentinel(const Event& event) {
  return event.sequence == 77777 && event.epoch == 99999 &&
         strcmp(event.uid, "DEADBEEF") == 0 && event.type == 'X' &&
         event.seconds == 4242;
}

static bool allAscii(const char* text) {
  if (text == nullptr) {
    return false;
  }
  for (const unsigned char* p = reinterpret_cast<const unsigned char*>(text);
       *p != 0; ++p) {
    if (*p >= 0x80) {
      return false;
    }
  }
  return true;
}

static void makeUid(char uid[9], unsigned index) {
  std::snprintf(uid, 9, "A1B2%04X", index);
}

// Live scan: preview then commit, optionally recording the journal event.
static Result scan(Attendance& attendance, const char* uid, uint32_t epoch,
                   Event* recorded = nullptr) {
  Event event;
  const Result previewResult = attendance.preview(uid, epoch, event);
  if (previewResult != Result::Ok) {
    return previewResult;
  }
  if (recorded != nullptr) {
    *recorded = event;
  }
  return attendance.commit(event);
}

static void testNormalizeUid() {
  char out[21];
  memset(out, 'x', sizeof(out));

  CHECK(Attendance::normalizeUid("a1b2c3d4", out));
  CHECK(strcmp(out, "A1B2C3D4") == 0);
  CHECK(Attendance::normalizeUid("a1b2c3d4e5f607", out));
  CHECK(strcmp(out, "A1B2C3D4E5F607") == 0);
  CHECK(Attendance::normalizeUid("a1b2c3d4e5f60718293a", out));
  CHECK(strcmp(out, "A1B2C3D4E5F60718293A") == 0);
  CHECK(Attendance::normalizeUid("A1B2C3D4", out));
  CHECK(strcmp(out, "A1B2C3D4") == 0);
  CHECK(Attendance::normalizeUid("0123456789abcd", out));
  CHECK(strcmp(out, "0123456789ABCD") == 0);
  CHECK(!Attendance::normalizeUid("0123456789abcdef", out));  // 16 chars

  CHECK(!Attendance::normalizeUid(nullptr, out));
  CHECK(!Attendance::normalizeUid("A1B2C3D4", nullptr));

  const char* invalid[] = {
      "",
      "0123456789abcdef",     // 16
      "A1B2C3D",              // 7
      "A1B2C3D4E",            // 9
      "A1B2C3D4E5F60",        // 13
      "A1B2C3D4E5F6071",      // 15
      "A1B2C3D4E5F6071829",   // 19
      "A1B2C3D4E5F60718293AB",  // 21
      "G1B2C3D4",
      "A1B2C3D-",
      "A1 B2C3D4",
      "A1:B2:C3:D4",
      "a1b2c3d4\n",
  };
  for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
    CHECK(!Attendance::normalizeUid(invalid[i], out));
  }
}

static void testInvalidNames() {
  Attendance attendance;
  const char* uid = "A1B2C3D4";

  CHECK_RESULT(attendance.upsertEmployee(uid, nullptr), Result::InvalidName);
  CHECK_RESULT(attendance.upsertEmployee(uid, ""), Result::InvalidName);
  CHECK_RESULT(attendance.upsertEmployee(uid, "   "), Result::InvalidName);
  CHECK_RESULT(attendance.upsertEmployee(uid, "\t\r\n "), Result::InvalidName);
  CHECK_RESULT(attendance.upsertEmployee(uid, "\x01" "abc"),
               Result::InvalidName);
  CHECK_RESULT(attendance.upsertEmployee(uid, "\x1F" "abc"),
               Result::InvalidName);
  CHECK_RESULT(attendance.upsertEmployee(uid, "abc\x7F"), Result::InvalidName);
  CHECK_RESULT(attendance.upsertEmployee(uid, "abc\x01" "def"),
               Result::InvalidName);
  CHECK(attendance.count() == 0);

  char name48[49];
  memset(name48, 'A', 48);
  name48[48] = '\0';
  CHECK_RESULT(attendance.upsertEmployee(uid, name48), Result::Ok);
  CHECK(attendance.count() == 1);
  const Employee* employee = attendance.find(uid);
  CHECK(employee != nullptr);
  CHECK(strlen(employee->name) == 48);

  char name49[50];
  memset(name49, 'A', 49);
  name49[49] = '\0';
  Attendance other;
  CHECK_RESULT(other.upsertEmployee(uid, name49), Result::InvalidName);
  CHECK(other.count() == 0);

  Attendance utf8;
  CHECK_RESULT(utf8.upsertEmployee(uid, "\xC3\x9C" "mit"), Result::Ok);
  const Employee* utf8Employee = utf8.find(uid);
  CHECK(utf8Employee != nullptr);
  CHECK(strcmp(utf8Employee->name, "\xC3\x9C" "mit") == 0);

  Attendance padded;
  CHECK_RESULT(padded.upsertEmployee(uid, " Ali "), Result::Ok);
  CHECK(padded.find(uid) != nullptr);
  CHECK(strcmp(padded.find(uid)->name, " Ali ") == 0);
}

static void testRegistrationCap() {
  Attendance attendance;
  char uid[9];
  for (unsigned i = 0; i < MAX_EMPLOYEES; ++i) {
    makeUid(uid, i);
    CHECK_RESULT(attendance.upsertEmployee(uid, "Personel"), Result::Ok);
    CHECK(attendance.count() == i + 1);
  }
  CHECK(attendance.count() == 32);

  makeUid(uid, 32);
  CHECK_RESULT(attendance.upsertEmployee(uid, "Fazla"), Result::Full);
  CHECK(attendance.count() == 32);
  CHECK(attendance.find(uid) == nullptr);

  // Renaming an existing employee must still work at capacity.
  makeUid(uid, 7);
  CHECK_RESULT(attendance.upsertEmployee(uid, "Yeni Isim"), Result::Ok);
  CHECK(attendance.count() == 32);
  CHECK(strcmp(attendance.find(uid)->name, "Yeni Isim") == 0);

  CHECK(attendance.at(31) != nullptr);
  CHECK(attendance.at(32) == nullptr);
  CHECK(attendance.at(100) == nullptr);
}

static void testLookupAndResultText() {
  Attendance empty;
  CHECK(empty.count() == 0);
  CHECK(empty.find(nullptr) == nullptr);
  CHECK(empty.find("A1B2C3D4") == nullptr);
  CHECK(empty.at(0) == nullptr);
  CHECK(!empty.anyInside());

  CHECK_RESULT(empty.upsertEmployee(nullptr, "Ali"), Result::InvalidUid);
  CHECK_RESULT(empty.upsertEmployee("bad", "Ali"), Result::InvalidUid);
  CHECK(empty.count() == 0);

  CHECK_RESULT(empty.upsertEmployee("a1b2c3d4", "Ali"), Result::Ok);
  const Employee* byLower = empty.find("a1b2c3d4");
  CHECK(byLower != nullptr);
  CHECK(strcmp(byLower->uid, "A1B2C3D4") == 0);
  CHECK(empty.find("A1B2C3D4") == byLower);
  CHECK(empty.find("A1B2C3D5") == nullptr);
  CHECK(empty.find("") == nullptr);
  CHECK(empty.find(nullptr) == nullptr);
  CHECK(empty.at(0) == byLower);
  CHECK(empty.at(1) == nullptr);

  const Result values[] = {
      Result::Ok,       Result::InvalidUid,   Result::InvalidName,
      Result::Full,     Result::UnknownCard,  Result::TooSoon,
      Result::ClockBackwards, Result::InvalidEvent,
  };
  for (size_t i = 0; i < sizeof(values) / sizeof(values[0]); ++i) {
    const char* text = resultText(values[i]);
    CHECK(text != nullptr);
    CHECK(strlen(text) > 0);
    CHECK(allAscii(text));
  }
  CHECK(resultText(static_cast<Result>(200)) != nullptr);
  CHECK(allAscii(resultText(static_cast<Result>(200))));
}

static void testUnknownCardAndZeroEpoch() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);

  Event event;
  sentinel(event);
  CHECK_RESULT(attendance.preview("FFFFFFFF", 1000, event),
               Result::UnknownCard);
  CHECK(isSentinel(event));

  sentinel(event);
  CHECK_RESULT(attendance.preview("bad", 1000, event), Result::InvalidUid);
  CHECK(isSentinel(event));

  sentinel(event);
  CHECK_RESULT(attendance.preview(nullptr, 1000, event), Result::InvalidUid);
  CHECK(isSentinel(event));

  sentinel(event);
  CHECK_RESULT(attendance.preview("A1B2C3D4", 0, event), Result::InvalidEvent);
  CHECK(isSentinel(event));
  CHECK(attendance.lastSequence() == 0);
  CHECK(attendance.lastEpoch() == 0);
}

static void testEntryExit() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("a1b2c3d4", "Ali"), Result::Ok);

  Event event;
  sentinel(event);
  CHECK_RESULT(attendance.preview("a1b2c3d4", 1000, event), Result::Ok);
  CHECK(event.sequence == 1);
  CHECK(event.epoch == 1000);
  CHECK(strcmp(event.uid, "A1B2C3D4") == 0);
  CHECK(event.type == 'I');
  CHECK(event.seconds == 0);
  // Preview never mutates state.
  CHECK(attendance.lastSequence() == 0);
  CHECK(attendance.lastEpoch() == 0);
  CHECK(!attendance.find("A1B2C3D4")->inside);

  CHECK_RESULT(attendance.commit(event), Result::Ok);
  const Employee* employee = attendance.find("A1B2C3D4");
  CHECK(employee != nullptr);
  CHECK(employee->inside);
  CHECK(employee->entryEpoch == 1000);
  CHECK(employee->lastScanEpoch == 1000);
  CHECK(employee->totalSeconds == 0);
  CHECK(attendance.lastSequence() == 1);
  CHECK(attendance.lastEpoch() == 1000);
  CHECK(attendance.anyInside());

  // Commit of the same journal entry again must be rejected without mutation.
  CHECK_RESULT(attendance.commit(event), Result::InvalidEvent);
  CHECK(attendance.lastSequence() == 1);
  CHECK(attendance.lastEpoch() == 1000);
  CHECK(attendance.find("A1B2C3D4")->inside);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 0);

  CHECK_RESULT(attendance.preview("A1B2C3D4", 1010, event), Result::Ok);
  CHECK(event.sequence == 2);
  CHECK(event.type == 'O');
  CHECK(event.seconds == 10);
  CHECK_RESULT(attendance.commit(event), Result::Ok);

  employee = attendance.find("A1B2C3D4");
  CHECK(!employee->inside);
  CHECK(employee->entryEpoch == 0);
  CHECK(employee->lastScanEpoch == 1010);
  CHECK(employee->totalSeconds == 10);
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 1010);
  CHECK(!attendance.anyInside());
}

static void testDebounceBoundary() {
  CHECK(RESCAN_SECONDS == 5);
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);

  Event event;
  sentinel(event);
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1004, event), Result::TooSoon);
  CHECK(isSentinel(event));
  CHECK(attendance.lastSequence() == 1);
  CHECK(attendance.lastEpoch() == 1000);
  CHECK(attendance.find("A1B2C3D4")->inside);

  CHECK_RESULT(attendance.preview("A1B2C3D4", 1005, event), Result::Ok);
  CHECK(event.type == 'O');
  CHECK(event.seconds == 5);
  CHECK_RESULT(attendance.commit(event), Result::Ok);

  sentinel(event);
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1009, event), Result::TooSoon);
  CHECK(isSentinel(event));
  CHECK(attendance.lastSequence() == 2);

  CHECK_RESULT(attendance.preview("A1B2C3D4", 1010, event), Result::Ok);
  CHECK(event.type == 'I');
  CHECK(event.seconds == 0);
  CHECK_RESULT(attendance.commit(event), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->inside);
  CHECK(attendance.find("A1B2C3D4")->entryEpoch == 1010);
}

static void testRenameWhileInsidePreservesState() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);

  Snapshot before = take(attendance, "A1B2C3D4");
  CHECK_RESULT(attendance.upsertEmployee("a1b2c3d4", "Veli"), Result::Ok);
  Snapshot after = take(attendance, "A1B2C3D4");
  CHECK(strcmp(after.name, "Veli") == 0);
  CHECK(after.inside == before.inside);
  CHECK(after.entryEpoch == before.entryEpoch);
  CHECK(after.lastScanEpoch == before.lastScanEpoch);
  CHECK(after.totalSeconds == before.totalSeconds);
  CHECK(after.sequence == before.sequence);
  CHECK(after.count == 1);

  // Self-alias and overlapping source: rename must not read erased bytes.
  const Employee* alias = attendance.find("A1B2C3D4");
  CHECK(alias != nullptr);
  CHECK_RESULT(attendance.upsertEmployee(alias->uid, alias->name), Result::Ok);
  CHECK(strcmp(attendance.find("A1B2C3D4")->name, "Veli") == 0);

  alias = attendance.find("A1B2C3D4");
  CHECK(alias != nullptr);
  CHECK_RESULT(attendance.upsertEmployee(alias->uid, alias->name + 1),
               Result::Ok);
  CHECK(strcmp(attendance.find("A1B2C3D4")->name, "eli") == 0);
  const Employee* renamed = attendance.find("A1B2C3D4");
  CHECK(renamed->inside);
  CHECK(renamed->entryEpoch == 1000);
  CHECK(renamed->lastScanEpoch == 1000);
  CHECK(renamed->totalSeconds == 0);
  CHECK(attendance.lastSequence() == 1);

  Event event;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1010, event), Result::Ok);
  CHECK(event.seconds == 10);
  CHECK_RESULT(attendance.commit(event), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 10);

  // Rename after exit keeps accumulated totals and never consumes a sequence.
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ayse"), Result::Ok);
  const Employee* employee = attendance.find("A1B2C3D4");
  CHECK(strcmp(employee->name, "Ayse") == 0);
  CHECK(!employee->inside);
  CHECK(employee->totalSeconds == 10);
  CHECK(employee->lastScanEpoch == 1010);
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.count() == 1);
}

static void testClockRollback() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(attendance.upsertEmployee("B1B2C3D4", "Veli"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 2000), Result::Ok);

  Event event;
  sentinel(event);
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1999, event),
               Result::ClockBackwards);
  CHECK(isSentinel(event));

  // Global monotonicity also applies to other employees.
  sentinel(event);
  CHECK_RESULT(attendance.preview("B1B2C3D4", 1999, event),
               Result::ClockBackwards);
  CHECK(isSentinel(event));

  // Equal to the global last epoch is allowed.
  CHECK_RESULT(attendance.preview("B1B2C3D4", 2000, event), Result::Ok);
  CHECK(event.sequence == 2);
  CHECK(event.type == 'I');
  CHECK_RESULT(attendance.commit(event), Result::Ok);

  // A crafted journal entry with a backwards clock is rejected unchanged.
  Event crafted;
  crafted.sequence = 3;
  crafted.epoch = 1999;
  memcpy(crafted.uid, "A1B2C3D4", 9);
  crafted.type = 'O';
  crafted.seconds = 0;
  Snapshot before = take(attendance, "A1B2C3D4");
  CHECK_RESULT(attendance.commit(crafted), Result::ClockBackwards);
  CHECK(same(take(attendance, "A1B2C3D4"), before));
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 2000);
  CHECK(attendance.find("A1B2C3D4")->inside);
  CHECK(attendance.find("A1B2C3D4")->entryEpoch == 2000);
}

static void testNightDayBoundary() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Gece"), Result::Ok);

  // 23:59:50 -> 00:00:20: arithmetic is epoch based, independent of the day.
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 86390), Result::Ok);
  Event event;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 86420, event), Result::Ok);
  CHECK(event.type == 'O');
  CHECK(event.seconds == 30);
  CHECK_RESULT(attendance.commit(event), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 30);

  // Midnight entry, next-midnight exit spans a full day.
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 86430), Result::Ok);
  CHECK_RESULT(attendance.preview("A1B2C3D4", 172800, event), Result::Ok);
  CHECK(event.seconds == 86370);
  CHECK_RESULT(attendance.commit(event), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 30 + 86370);
}

static void testMultiEmployeeIndependence() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(attendance.upsertEmployee("B1B2C3D4", "Veli"), Result::Ok);

  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);
  CHECK_RESULT(scan(attendance, "B1B2C3D4", 1006), Result::Ok);
  CHECK(attendance.anyInside());
  CHECK(attendance.lastSequence() == 2);

  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1010), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 10);
  CHECK(attendance.find("A1B2C3D4")->inside == false);
  CHECK(attendance.find("B1B2C3D4")->inside);
  CHECK(attendance.find("B1B2C3D4")->entryEpoch == 1006);
  CHECK(attendance.find("B1B2C3D4")->totalSeconds == 0);

  // Debounce is per employee: A's scan does not delay B.
  Event event;
  CHECK_RESULT(attendance.preview("B1B2C3D4", 1010, event), Result::TooSoon);
  CHECK_RESULT(attendance.preview("B1B2C3D4", 1011, event), Result::Ok);
  CHECK(event.type == 'O');
  CHECK(event.seconds == 5);
  CHECK_RESULT(attendance.commit(event), Result::Ok);
  CHECK(attendance.find("B1B2C3D4")->totalSeconds == 5);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 10);
  CHECK(!attendance.anyInside());

  // A can re-enter after its own 5 second window, unaffected by B.
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1020, event), Result::Ok);
  CHECK(event.sequence == 5);
  CHECK(event.type == 'I');
  CHECK_RESULT(attendance.commit(event), Result::Ok);
  CHECK(attendance.anyInside());
  CHECK(attendance.find("B1B2C3D4")->inside == false);
}

static void testCommitValidation() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);
  Snapshot before = take(attendance, "A1B2C3D4");

  Event event;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1010, event), Result::Ok);
  CHECK(event.type == 'O');
  CHECK(event.seconds == 10);

  Event bad = event;
  memcpy(bad.uid, "a1b2c3d4", 9);
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  bad = event;
  memcpy(bad.uid, "bad", 4);
  CHECK_RESULT(attendance.commit(bad), Result::InvalidUid);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  bad = event;
  memcpy(bad.uid, "FFFFFFFF", 9);
  CHECK_RESULT(attendance.commit(bad), Result::UnknownCard);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  bad = event;
  bad.epoch = 0;
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  bad = event;
  bad.sequence = 1;
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  bad = event;
  bad.sequence = 3;  // Gap: next expected is 2.
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  bad = event;
  bad.type = 'I';
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  bad = event;
  bad.type = 'X';
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  bad = event;
  bad.seconds = 9;
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  bad = event;
  bad.seconds = 11;
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(same(take(attendance, "A1B2C3D4"), before));

  // Original preview event is still valid and commits.
  CHECK_RESULT(attendance.commit(event), Result::Ok);
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 1010);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 10);
  CHECK(!attendance.anyInside());
}

static void testReplayAndSequenceGap() {
  Attendance live;
  CHECK_RESULT(live.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(live.upsertEmployee("B1B2C3D4", "Veli"), Result::Ok);
  CHECK_RESULT(live.upsertEmployee("C1B2C3D4", "Ayse"), Result::Ok);

  Event journal[8];
  size_t journalCount = 0;
  CHECK_RESULT(scan(live, "A1B2C3D4", 1000, &journal[journalCount++]),
               Result::Ok);
  CHECK_RESULT(scan(live, "B1B2C3D4", 1005, &journal[journalCount++]),
               Result::Ok);
  CHECK_RESULT(scan(live, "A1B2C3D4", 1010, &journal[journalCount++]),
               Result::Ok);
  CHECK_RESULT(scan(live, "B1B2C3D4", 1016, &journal[journalCount++]),
               Result::Ok);
  CHECK_RESULT(scan(live, "C1B2C3D4", 1030, &journal[journalCount++]),
               Result::Ok);
  CHECK_RESULT(scan(live, "A1B2C3D4", 1100, &journal[journalCount++]),
               Result::Ok);
  CHECK(journalCount == 6);
  CHECK(live.lastSequence() == 6);
  CHECK(live.lastEpoch() == 1100);

  // Simulated reboot: same registry, journal replayed through commit.
  Attendance restored;
  CHECK_RESULT(restored.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(restored.upsertEmployee("B1B2C3D4", "Veli"), Result::Ok);
  CHECK_RESULT(restored.upsertEmployee("C1B2C3D4", "Ayse"), Result::Ok);

  for (size_t i = 0; i < journalCount; ++i) {
    CHECK_RESULT(restored.commit(journal[i]), Result::Ok);
  }
  CHECK(restored.count() == live.count());
  CHECK(restored.lastSequence() == live.lastSequence());
  CHECK(restored.lastEpoch() == live.lastEpoch());
  CHECK(restored.anyInside());
  for (size_t i = 0; i < restored.count(); ++i) {
    const Employee* expected = live.find(live.at(i)->uid);
    const Employee* actual = restored.find(restored.at(i)->uid);
    CHECK(expected != nullptr);
    CHECK(actual != nullptr);
    if (expected != nullptr && actual != nullptr) {
      CHECK(strcmp(expected->uid, actual->uid) == 0);
      CHECK(strcmp(expected->name, actual->name) == 0);
      CHECK(expected->inside == actual->inside);
      CHECK(expected->entryEpoch == actual->entryEpoch);
      CHECK(expected->lastScanEpoch == actual->lastScanEpoch);
      CHECK(expected->totalSeconds == actual->totalSeconds);
    }
  }

  // Replaying the same journal again must fail cleanly.
  Snapshot restoredState = take(restored, "A1B2C3D4");
  CHECK_RESULT(restored.commit(journal[0]), Result::InvalidEvent);
  CHECK(same(take(restored, "A1B2C3D4"), restoredState));
  CHECK(restored.lastSequence() == 6);

  // Continue after replay: next live scan uses sequence 7 and current state.
  Event next;
  CHECK_RESULT(restored.preview("A1B2C3D4", 1110, next), Result::Ok);
  CHECK(next.sequence == 7);
  CHECK(next.type == 'O');
  CHECK(next.seconds == 10);
  CHECK_RESULT(restored.commit(next), Result::Ok);

  // Corrupt replay entries must not mutate state.
  Attendance replay;
  CHECK_RESULT(replay.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(replay.upsertEmployee("B1B2C3D4", "Veli"), Result::Ok);
  CHECK_RESULT(replay.upsertEmployee("C1B2C3D4", "Ayse"), Result::Ok);
  CHECK_RESULT(replay.commit(journal[0]), Result::Ok);
  CHECK_RESULT(replay.commit(journal[1]), Result::Ok);

  Snapshot replayState = take(replay, "A1B2C3D4");

  Event corrupt = journal[2];
  corrupt.type = 'I';
  CHECK_RESULT(replay.commit(corrupt), Result::InvalidEvent);
  CHECK(same(take(replay, "A1B2C3D4"), replayState));

  corrupt = journal[2];
  corrupt.seconds = 9;
  CHECK_RESULT(replay.commit(corrupt), Result::InvalidEvent);
  CHECK(same(take(replay, "A1B2C3D4"), replayState));

  corrupt = journal[2];
  corrupt.sequence = 4;
  CHECK_RESULT(replay.commit(corrupt), Result::InvalidEvent);
  CHECK(same(take(replay, "A1B2C3D4"), replayState));

  corrupt = journal[2];
  corrupt.epoch = 1004;
  CHECK_RESULT(replay.commit(corrupt), Result::ClockBackwards);
  CHECK(same(take(replay, "A1B2C3D4"), replayState));

  corrupt = journal[2];
  memcpy(corrupt.uid, "a1b2c3d4", 9);
  CHECK_RESULT(replay.commit(corrupt), Result::InvalidEvent);
  CHECK(same(take(replay, "A1B2C3D4"), replayState));

  corrupt = journal[2];
  corrupt.epoch = 0;
  CHECK_RESULT(replay.commit(corrupt), Result::InvalidEvent);
  CHECK(same(take(replay, "A1B2C3D4"), replayState));

  corrupt = journal[2];
  memcpy(corrupt.uid, "bad", 4);
  CHECK_RESULT(replay.commit(corrupt), Result::InvalidUid);
  CHECK(same(take(replay, "A1B2C3D4"), replayState));

  // Unknown but well-formed card in the journal.
  corrupt = journal[2];
  memcpy(corrupt.uid, "FFFFFFFF", 9);
  CHECK_RESULT(replay.commit(corrupt), Result::UnknownCard);
  CHECK(same(take(replay, "A1B2C3D4"), replayState));

  CHECK(replay.lastSequence() == 2);
  CHECK(replay.lastEpoch() == 1005);

  // The pristine entry still commits afterwards.
  CHECK_RESULT(replay.commit(journal[2]), Result::Ok);
  CHECK(replay.lastSequence() == 3);
  CHECK(replay.lastEpoch() == 1010);
}

static void testSequenceGapFromStart() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);

  Event event;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1000, event), Result::Ok);
  CHECK(event.sequence == 1);

  Event bad = event;
  bad.sequence = 0;
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(attendance.lastSequence() == 0);
  CHECK(attendance.lastEpoch() == 0);
  CHECK(!attendance.find("A1B2C3D4")->inside);

  bad = event;
  bad.sequence = 2;
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(attendance.lastSequence() == 0);
  CHECK(attendance.lastEpoch() == 0);

  CHECK_RESULT(attendance.commit(event), Result::Ok);
  CHECK(attendance.lastSequence() == 1);

  Event gap = event;
  gap.sequence = 3;
  gap.epoch = 1010;
  gap.type = 'O';
  gap.seconds = 10;
  CHECK_RESULT(attendance.commit(gap), Result::InvalidEvent);
  CHECK(attendance.lastSequence() == 1);
  CHECK(attendance.lastEpoch() == 1000);
}

static void testNoGlobalCooldown() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(attendance.upsertEmployee("B1B2C3D4", "Veli"), Result::Ok);
  CHECK_RESULT(attendance.upsertEmployee("C1B2C3D4", "Ayse"), Result::Ok);
  // Independent people can scan in the exact same second.
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);
  CHECK_RESULT(scan(attendance, "B1B2C3D4", 1000), Result::Ok);
  Event event;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1004, event), Result::TooSoon);
  CHECK_RESULT(attendance.preview("B1B2C3D4", 1004, event), Result::TooSoon);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1005), Result::Ok);
  CHECK_RESULT(scan(attendance, "B1B2C3D4", 1005), Result::Ok);
  CHECK_RESULT(scan(attendance, "C1B2C3D4", 1005), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 5);
  CHECK(attendance.find("B1B2C3D4")->totalSeconds == 5);
  CHECK(attendance.find("C1B2C3D4")->inside);
  CHECK_RESULT(attendance.preview("B1B2C3D4", 1009, event), Result::TooSoon);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1010), Result::Ok);
  CHECK_RESULT(scan(attendance, "B1B2C3D4", 1010), Result::Ok);
}

static void testUint64Totals() {
  // Field width and accumulation must stay 64-bit.
  CHECK(sizeof(((Employee*)0)->totalSeconds) == 8);

  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Uzun Vardiya"),
               Result::Ok);

  CHECK_RESULT(scan(attendance, "A1B2C3D4", 10), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 4000000000U), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds ==
        static_cast<uint64_t>(3999999990ULL));

  CHECK_RESULT(scan(attendance, "A1B2C3D4", 4000000010U), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 4000000020U), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds ==
        static_cast<uint64_t>(4000000000ULL));

  // Multiple cycles accumulate exactly (no truncation or reset).
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 4000000030U), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 4000000045U), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds ==
        static_cast<uint64_t>(4000000015ULL));
}

int main() {
  testNormalizeUid();
  testInvalidNames();
  testRegistrationCap();
  testLookupAndResultText();
  testUnknownCardAndZeroEpoch();
  testEntryExit();
  testDebounceBoundary();
  testRenameWhileInsidePreservesState();
  testClockRollback();
  testNightDayBoundary();
  testMultiEmployeeIndependence();
  testNoGlobalCooldown();
  testCommitValidation();
  testReplayAndSequenceGap();
  testSequenceGapFromStart();
  testUint64Totals();

  std::printf("%d checks, %d failure(s)\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
