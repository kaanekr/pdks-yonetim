// Native tests for named/archived journal rows and permanent roster removal.
// Pure C++: no Arduino, no filesystem, no Storage dependency.
// Build (host): g++ -std=c++11 -Iinclude src/Attendance.cpp test/test_named_core.cpp -o named_core_tests

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

struct CoreState {
  bool found = false;
  size_t count = 0;
  uint32_t sequence = 0;
  uint32_t lastEpoch = 0;
  bool inside = false;
  uint32_t entryEpoch = 0;
  uint32_t lastScanEpoch = 0;
  uint64_t totalSeconds = 0;
  char name[49] = {};
};

static CoreState stateOf(const Attendance& attendance, const char* uid) {
  CoreState state;
  state.count = attendance.count();
  state.sequence = attendance.lastSequence();
  state.lastEpoch = attendance.lastEpoch();
  const Employee* employee = attendance.find(uid);
  if (employee != nullptr) {
    state.found = true;
    state.inside = employee->inside;
    state.entryEpoch = employee->entryEpoch;
    state.lastScanEpoch = employee->lastScanEpoch;
    state.totalSeconds = employee->totalSeconds;
    memcpy(state.name, employee->name, sizeof(state.name));
  }
  return state;
}

static bool sameState(const CoreState& left, const CoreState& right) {
  return left.found == right.found && left.count == right.count &&
         left.sequence == right.sequence && left.lastEpoch == right.lastEpoch &&
         left.inside == right.inside && left.entryEpoch == right.entryEpoch &&
         left.lastScanEpoch == right.lastScanEpoch &&
         left.totalSeconds == right.totalSeconds &&
         strcmp(left.name, right.name) == 0;
}

static void makeUid(char uid[9], unsigned index) {
  std::snprintf(uid, 9, "A1B2%04X", index);
}

// Live scan: preview then commit.
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

static void putUid(Event& event, const char* uid) {
  memset(event.uid, 0, sizeof(event.uid));
  memcpy(event.uid, uid, strlen(uid));
}

static void putName(Event& event, const char* name) {
  memset(event.name, 0, sizeof(event.name));
  memcpy(event.name, name, strlen(name));
}

// Synthetic historical row of a removed employee.
static Event archiveRow(uint32_t sequence, uint32_t epoch, const char* uid,
                        char type, uint32_t seconds, const char* name) {
  Event event;
  event.sequence = sequence;
  event.epoch = epoch;
  putUid(event, uid);
  event.type = type;
  event.seconds = seconds;
  putName(event, name);
  event.nameRecorded = true;
  event.archived = true;
  return event;
}

static void testEventShapeAndPreviewSnapshot() {
  const Event fresh;
  CHECK(fresh.name[0] == '\0');
  CHECK(!fresh.nameRecorded);
  CHECK(!fresh.archived);

  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("a1b2c3d4", "Ali"), Result::Ok);

  Event entry;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1000, entry), Result::Ok);
  CHECK(entry.nameRecorded);
  CHECK(strcmp(entry.name, "Ali") == 0);
  CHECK(!entry.archived);
  CHECK(entry.type == 'I');
  CHECK(entry.seconds == 0);
  CHECK_RESULT(attendance.commit(entry), Result::Ok);

  Event exitRow;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1010, exitRow), Result::Ok);
  CHECK(exitRow.nameRecorded);
  CHECK(strcmp(exitRow.name, "Ali") == 0);
  CHECK(exitRow.type == 'O');
  CHECK(exitRow.seconds == 10);
  CHECK_RESULT(attendance.commit(exitRow), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 10);
}

static void testLegacyRowsWithoutName() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);

  Event entry;
  entry.sequence = 1;
  entry.epoch = 1000;
  putUid(entry, "A1B2C3D4");
  entry.type = 'I';
  entry.seconds = 0;
  CHECK(!entry.nameRecorded);
  CHECK(entry.name[0] == '\0');
  CHECK(!entry.archived);
  CHECK_RESULT(attendance.commit(entry), Result::Ok);

  Event exitRow;
  exitRow.sequence = 2;
  exitRow.epoch = 1010;
  putUid(exitRow, "A1B2C3D4");
  exitRow.type = 'O';
  exitRow.seconds = 10;
  CHECK_RESULT(attendance.commit(exitRow), Result::Ok);

  const Employee* employee = attendance.find("A1B2C3D4");
  CHECK(employee != nullptr);
  CHECK(strcmp(employee->name, "Ali") == 0);
  CHECK(employee->totalSeconds == 10);
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 1010);
}

static void testRenameKeepsEventSnapshots() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);

  Event entry;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1000, entry), Result::Ok);
  CHECK(strcmp(entry.name, "Ali") == 0);

  // Rename before the row is committed must not rewrite the pending row.
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Veli"), Result::Ok);
  CHECK(strcmp(entry.name, "Ali") == 0);
  CHECK(strcmp(attendance.find("A1B2C3D4")->name, "Veli") == 0);

  CHECK_RESULT(attendance.commit(entry), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->inside);
  CHECK(strcmp(attendance.find("A1B2C3D4")->name, "Veli") == 0);

  Event exitRow;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1010, exitRow), Result::Ok);
  CHECK(strcmp(exitRow.name, "Veli") == 0);
  CHECK(exitRow.seconds == 10);

  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ayse"), Result::Ok);
  CHECK(strcmp(exitRow.name, "Veli") == 0);
  CHECK_RESULT(attendance.commit(exitRow), Result::Ok);

  const Employee* employee = attendance.find("A1B2C3D4");
  CHECK(strcmp(employee->name, "Ayse") == 0);
  CHECK(employee->totalSeconds == 10);
  CHECK(attendance.count() == 1);
}

static void testCommitNameValidation() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);

  Event base;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1000, base), Result::Ok);
  const CoreState before = stateOf(attendance, "A1B2C3D4");

  Event bad = base;
  bad.name[0] = '\0';
  bad.nameRecorded = true;
  CHECK_RESULT(attendance.commit(bad), Result::InvalidName);
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));

  bad = base;
  putName(bad, "   ");
  bad.nameRecorded = false;
  CHECK_RESULT(attendance.commit(bad), Result::InvalidName);
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));

  bad = base;
  putName(bad, "\x01X");
  CHECK_RESULT(attendance.commit(bad), Result::InvalidName);
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));

  bad = base;
  putName(bad, "abc\x7F");
  CHECK_RESULT(attendance.commit(bad), Result::InvalidName);
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));

  bad = base;
  memset(bad.name, 'A', sizeof(bad.name)); // no terminator inside the 49 bytes
  CHECK_RESULT(attendance.commit(bad), Result::InvalidName);
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));

  // An empty name is still a valid legacy row (rejected here for the sequence).
  bad = base;
  bad.name[0] = '\0';
  bad.nameRecorded = false;
  bad.sequence = 9;
  CHECK_RESULT(attendance.commit(bad), Result::InvalidEvent);
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));

  CHECK_RESULT(attendance.commit(base), Result::Ok);
  CHECK(attendance.lastSequence() == 1);
  CHECK(strcmp(attendance.find("A1B2C3D4")->name, "Ali") == 0);
}

static void testRecoveryNeverCarriesName() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);

  Event recovery;
  CHECK_RESULT(attendance.previewRecovery(2000, recovery), Result::Ok);
  CHECK(recovery.type == 'R');
  CHECK(strcmp(recovery.uid, "00000000") == 0);
  CHECK(recovery.seconds == 1000);
  CHECK(recovery.name[0] == '\0');
  CHECK(!recovery.nameRecorded);
  CHECK(!recovery.archived);

  CHECK_RESULT(attendance.commit(recovery), Result::Ok);
  const Employee* employee = attendance.find("A1B2C3D4");
  CHECK(employee != nullptr);
  CHECK(strcmp(employee->name, "Ali") == 0);
  CHECK(!employee->inside);
  CHECK(employee->totalSeconds == 0);
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 2000);
}

static void testManualSnapshotAndRename() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);

  Event blocked;
  CHECK_RESULT(attendance.previewManual("A1B2C3D4", 1200, 1800, blocked),
               Result::OpenSession);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1010), Result::Ok); // totals 10

  Event manual;
  CHECK_RESULT(attendance.previewManual("a1b2c3d4", 1200, 1800, manual),
               Result::Ok);
  CHECK(manual.sequence == 3);
  CHECK(manual.type == 'M');
  CHECK(manual.seconds == 600);
  CHECK(manual.nameRecorded);
  CHECK(strcmp(manual.name, "Ali") == 0);
  CHECK(!manual.archived);

  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Veli"), Result::Ok);
  Event renamed;
  CHECK_RESULT(attendance.previewManual("A1B2C3D4", 1200, 1800, renamed),
               Result::Ok);
  CHECK(strcmp(manual.name, "Ali") == 0);
  CHECK(strcmp(renamed.name, "Veli") == 0);

  CHECK_RESULT(attendance.commit(manual), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 610);
  CHECK(strcmp(attendance.find("A1B2C3D4")->name, "Veli") == 0);

  Event bad;
  CHECK_RESULT(attendance.previewManual("A1B2C3D4", 0, 1800, bad),
               Result::InvalidEvent);
  CHECK_RESULT(attendance.previewManual("A1B2C3D4", 1800, 1800, bad),
               Result::InvalidEvent);
  CHECK_RESULT(attendance.previewManual("FFFFFFFF", 1200, 1800, bad),
               Result::UnknownCard);
  CHECK(attendance.lastSequence() == 3);
}

static void testRemoveEmployeeRules() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(attendance.upsertEmployee("B1B2C3D4", "Veli"), Result::Ok);
  CHECK_RESULT(attendance.upsertEmployee("C1B2C3D4", "Ayse"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);

  const CoreState before = stateOf(attendance, "A1B2C3D4");
  CHECK_RESULT(attendance.removeEmployee("A1B2C3D4"), Result::OpenSession);
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));
  CHECK(attendance.count() == 3);
  CHECK(attendance.find("A1B2C3D4") != nullptr);

  CHECK_RESULT(attendance.removeEmployee(nullptr), Result::InvalidUid);
  CHECK_RESULT(attendance.removeEmployee("bad"), Result::InvalidUid);
  CHECK_RESULT(attendance.removeEmployee("FFFFFFFF"), Result::UnknownCard);
  CHECK(attendance.count() == 3);

  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1010), Result::Ok); // totals 10
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 1010);

  // Canonicalisation applies to the removal input too.
  CHECK_RESULT(attendance.removeEmployee("a1b2c3d4"), Result::Ok);
  CHECK(attendance.count() == 2);
  CHECK(attendance.find("A1B2C3D4") == nullptr);
  CHECK(attendance.findByCard("A1B2C3D4") == nullptr);
  // No journal bookkeeping changes on removal.
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 1010);

  // Surviving order is preserved after the shift.
  CHECK(attendance.at(0) != nullptr);
  CHECK(attendance.at(1) != nullptr);
  CHECK(strcmp(attendance.at(0)->uid, "B1B2C3D4") == 0);
  CHECK(strcmp(attendance.at(1)->uid, "C1B2C3D4") == 0);
  CHECK(attendance.at(2) == nullptr);
  CHECK(strcmp(attendance.at(0)->name, "Veli") == 0);
  CHECK(strcmp(attendance.at(1)->name, "Ayse") == 0);

  CHECK_RESULT(attendance.removeEmployee("C1B2C3D4"), Result::Ok);
  CHECK(attendance.count() == 1);
  CHECK_RESULT(attendance.removeEmployee("B1B2C3D4"), Result::Ok);
  CHECK(attendance.count() == 0);
  CHECK(attendance.at(0) == nullptr);
  CHECK(!attendance.anyInside());
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 1010);

  // A removed identity can no longer be looked up or scanned.
  Event probe;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1200, probe),
               Result::UnknownCard);
  CHECK_RESULT(attendance.previewManual("A1B2C3D4", 1200, 1800, probe),
               Result::UnknownCard);
}

static void testAddEmployeeContract() {
  Attendance attendance;
  CHECK_RESULT(attendance.addEmployee(nullptr, "Ali", "A1B2C3D4"),
               Result::InvalidUid);
  CHECK_RESULT(attendance.addEmployee("bad", "Ali", "A1B2C3D4"),
               Result::InvalidUid);
  CHECK_RESULT(attendance.addEmployee("A1B2C3D4", "Ali", nullptr),
               Result::InvalidUid);
  CHECK_RESULT(attendance.addEmployee("A1B2C3D4", "Ali", "bad"),
               Result::InvalidUid);
  CHECK_RESULT(attendance.addEmployee("A1B2C3D4", nullptr, "A1B2C3D4"),
               Result::InvalidName);
  CHECK_RESULT(attendance.addEmployee("A1B2C3D4", "   ", "A1B2C3D4"),
               Result::InvalidName);
  CHECK(attendance.count() == 0);

  // Explicit identity and card, both canonicalised.
  CHECK_RESULT(attendance.addEmployee("a1b2c3d4", "Ali", "deadbeef", true),
               Result::Ok);
  CHECK(attendance.count() == 1);
  const Employee* employee = attendance.find("A1B2C3D4");
  CHECK(employee != nullptr);
  CHECK(strcmp(employee->uid, "A1B2C3D4") == 0);
  CHECK(strcmp(employee->cardUid, "DEADBEEF") == 0);
  CHECK(employee->active);
  CHECK(!employee->inside);
  CHECK(employee->totalSeconds == 0);

  const CoreState before = stateOf(attendance, "A1B2C3D4");
  CHECK_RESULT(attendance.addEmployee("A1B2C3D4", "Kopya", "FFFFFFFF"),
               Result::CardInUse);
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));
  CHECK(attendance.count() == 1);

  CHECK_RESULT(attendance.addEmployee("B1B2C3D4", "Veli", "DEADBEEF"),
               Result::CardInUse);
  CHECK_RESULT(attendance.addEmployee("B1B2C3D4", "Veli", "A1B2C3D4"),
               Result::CardInUse);
  CHECK_RESULT(attendance.addEmployee("DEADBEEF", "Veli", "C1B2C3D4"),
               Result::CardInUse);
  CHECK(attendance.count() == 1);
  CHECK(attendance.find("B1B2C3D4") == nullptr);
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));

  // Inactive creation blocks scanning until the entry is activated.
  CHECK_RESULT(attendance.addEmployee("B1B2C3D4", "Veli", "B1B2C3D4", false),
               Result::Ok);
  CHECK(attendance.count() == 2);
  Event probe;
  CHECK_RESULT(attendance.preview("B1B2C3D4", 1000, probe),
               Result::InactiveCard);
  CHECK_RESULT(attendance.updateEmployee("B1B2C3D4", "Veli", "B1B2C3D4", true),
               Result::Ok);
  CHECK_RESULT(attendance.preview("B1B2C3D4", 1000, probe), Result::Ok);
  CHECK(strcmp(probe.name, "Veli") == 0);
  CHECK(!probe.archived);
}

static void testRemovedCardReissuedNewIdentity() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1010), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 10);

  CHECK_RESULT(attendance.removeEmployee("A1B2C3D4"), Result::Ok);
  CHECK(attendance.count() == 0);
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 1010);

  CHECK_RESULT(attendance.addEmployee("B2C3D4E5", "Veli", "A1B2C3D4"),
               Result::Ok);
  CHECK(attendance.count() == 1);
  const Employee* issued = attendance.findByCard("A1B2C3D4");
  CHECK(issued != nullptr);
  CHECK(strcmp(issued->uid, "B2C3D4E5") == 0);
  CHECK(strcmp(issued->name, "Veli") == 0);
  CHECK(issued->totalSeconds == 0);
  CHECK(!issued->inside);

  Event entry;
  CHECK_RESULT(attendance.preview("A1B2C3D4", 1100, entry), Result::Ok);
  CHECK(strcmp(entry.uid, "B2C3D4E5") == 0);
  CHECK(strcmp(entry.name, "Veli") == 0);
  CHECK(entry.sequence == 3);
  CHECK_RESULT(attendance.commit(entry), Result::Ok);
  CHECK(attendance.find("B2C3D4E5")->inside);
  CHECK(attendance.find("A1B2C3D4") == nullptr);
}

static void testArchivedHistoryAfterRemoval() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1010), Result::Ok);
  CHECK(attendance.find("A1B2C3D4")->totalSeconds == 10);

  CHECK_RESULT(attendance.removeEmployee("A1B2C3D4"), Result::Ok);
  CHECK(attendance.count() == 0);
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 1010);

  Event entry = archiveRow(3, 1020, "A1B2C3D4", 'I', 0, "Ali");
  CHECK_RESULT(attendance.commit(entry), Result::Ok);
  CHECK(attendance.count() == 0);
  CHECK(attendance.find("A1B2C3D4") == nullptr);
  CHECK(!attendance.anyInside());
  CHECK(attendance.lastSequence() == 3);
  CHECK(attendance.lastEpoch() == 1020);

  Event exitRow = archiveRow(4, 1040, "A1B2C3D4", 'O', 20, "Ali");
  CHECK_RESULT(attendance.commit(exitRow), Result::Ok);
  CHECK(attendance.count() == 0);
  CHECK(attendance.lastSequence() == 4);
  CHECK(attendance.lastEpoch() == 1040);

  Event manual = archiveRow(5, 1100, "A1B2C3D4", 'M', 60, "Ali");
  CHECK_RESULT(attendance.commit(manual), Result::Ok);
  CHECK(attendance.count() == 0);
  CHECK(attendance.lastSequence() == 5);
  CHECK(attendance.lastEpoch() == 1100);

  // Manual rows keep the max clock baseline, exactly like live ones.
  Event earlyManual = archiveRow(6, 1050, "A1B2C3D4", 'M', 30, "Ali");
  CHECK_RESULT(attendance.commit(earlyManual), Result::Ok);
  CHECK(attendance.lastSequence() == 6);
  CHECK(attendance.lastEpoch() == 1100);

  // A new person reuses the freed card only, not the archived identity.
  CHECK_RESULT(attendance.addEmployee("B2C3D4E5", "Veli", "A1B2C3D4"),
               Result::Ok);
  CHECK(attendance.count() == 1);
  const Employee* fresh = attendance.find("B2C3D4E5");
  CHECK(fresh != nullptr);
  CHECK(fresh->totalSeconds == 0);
  CHECK(!fresh->inside);

  // Archived rows never resurrect the removed identity, even after reissue.
  Event again = archiveRow(7, 1200, "A1B2C3D4", 'I', 0, "Ali");
  CHECK_RESULT(attendance.commit(again), Result::Ok);
  CHECK(attendance.count() == 1);
  CHECK(attendance.find("A1B2C3D4") == nullptr);
  CHECK(!attendance.find("B2C3D4E5")->inside);
  CHECK(attendance.find("B2C3D4E5")->totalSeconds == 0);
  CHECK(attendance.lastSequence() == 7);
  CHECK(attendance.lastEpoch() == 1200);
}

static void testArchivedValidation() {
  Attendance attendance;
  CHECK_RESULT(attendance.upsertEmployee("A1B2C3D4", "Ali"), Result::Ok);
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1000), Result::Ok);

  // A UID still on the roster cannot be marked archived.
  Event live = archiveRow(2, 1010, "A1B2C3D4", 'I', 0, "Ali");
  CHECK_RESULT(attendance.commit(live), Result::InvalidEvent);

  const CoreState before = stateOf(attendance, "A1B2C3D4");
  CHECK(before.sequence == 1);
  CHECK(before.lastEpoch == 1000);
  CHECK(before.inside);

  Event recovery = archiveRow(2, 1010, "00000000", 'R', 1000, "Ali");
  CHECK_RESULT(attendance.commit(recovery), Result::InvalidEvent);

  Event unknownType = archiveRow(2, 1010, "FFFFFFFF", 'X', 0, "Ali");
  CHECK_RESULT(attendance.commit(unknownType), Result::InvalidEvent);

  Event badUid = archiveRow(2, 1010, "bad", 'I', 0, "Ali");
  CHECK_RESULT(attendance.commit(badUid), Result::InvalidUid);

  Event lowerUid = archiveRow(2, 1010, "ffffffff", 'I', 0, "Ali");
  CHECK_RESULT(attendance.commit(lowerUid), Result::InvalidEvent);

  Event noName = archiveRow(2, 1010, "FFFFFFFF", 'I', 0, "");
  noName.nameRecorded = false;
  CHECK_RESULT(attendance.commit(noName), Result::InvalidName);

  Event blankName = archiveRow(2, 1010, "FFFFFFFF", 'I', 0, "   ");
  CHECK_RESULT(attendance.commit(blankName), Result::InvalidName);

  Event longName = archiveRow(2, 1010, "FFFFFFFF", 'I', 0, "Ali");
  memset(longName.name, 'A', sizeof(longName.name));
  CHECK_RESULT(attendance.commit(longName), Result::InvalidName);

  Event gap = archiveRow(4, 1010, "FFFFFFFF", 'I', 0, "Ali");
  CHECK_RESULT(attendance.commit(gap), Result::InvalidEvent);

  Event zeroEpoch = archiveRow(2, 0, "FFFFFFFF", 'I', 0, "Ali");
  CHECK_RESULT(attendance.commit(zeroEpoch), Result::InvalidEvent);

  Event entrySeconds = archiveRow(2, 1010, "FFFFFFFF", 'I', 5, "Ali");
  CHECK_RESULT(attendance.commit(entrySeconds), Result::InvalidEvent);

  Event exitZero = archiveRow(2, 1010, "FFFFFFFF", 'O', 0, "Ali");
  CHECK_RESULT(attendance.commit(exitZero), Result::InvalidEvent);

  Event exitTooLong = archiveRow(2, 1010, "FFFFFFFF", 'O', 1010, "Ali");
  CHECK_RESULT(attendance.commit(exitTooLong), Result::InvalidEvent);

  Event manualZero = archiveRow(2, 1010, "FFFFFFFF", 'M', 0, "Ali");
  CHECK_RESULT(attendance.commit(manualZero), Result::InvalidEvent);

  Event manualTooLong = archiveRow(2, 900, "FFFFFFFF", 'M', 900, "Ali");
  CHECK_RESULT(attendance.commit(manualTooLong), Result::InvalidEvent);

  // Every rejected row above left the core untouched.
  CHECK(sameState(stateOf(attendance, "A1B2C3D4"), before));
  CHECK(attendance.count() == 1);

  // Close and remove, then the same shapes are accepted as history.
  CHECK_RESULT(scan(attendance, "A1B2C3D4", 1010), Result::Ok);
  CHECK_RESULT(attendance.removeEmployee("A1B2C3D4"), Result::Ok);
  CHECK(attendance.count() == 0);
  CHECK(attendance.lastSequence() == 2);
  CHECK(attendance.lastEpoch() == 1010);

  Event archived = archiveRow(3, 1020, "FFFFFFFF", 'I', 0, "Ali");
  CHECK_RESULT(attendance.commit(archived), Result::Ok);
  CHECK(attendance.count() == 0);
  CHECK(attendance.lastSequence() == 3);
  CHECK(attendance.lastEpoch() == 1020);

  Event backwards = archiveRow(4, 1000, "FFFFFFFF", 'O', 20, "Ali");
  CHECK_RESULT(attendance.commit(backwards), Result::ClockBackwards);
  CHECK(attendance.lastSequence() == 3);
  CHECK(attendance.lastEpoch() == 1020);

  Event exitRow = archiveRow(4, 1040, "FFFFFFFF", 'O', 20, "Ali");
  CHECK_RESULT(attendance.commit(exitRow), Result::Ok);
  CHECK(attendance.lastSequence() == 4);
  CHECK(attendance.lastEpoch() == 1040);
  CHECK(attendance.count() == 0);
}

static void testRemovalFreesSlotAndKeepsState() {
  Attendance attendance;
  char uid[9];
  for (unsigned i = 0; i < MAX_EMPLOYEES; ++i) {
    makeUid(uid, i);
    CHECK_RESULT(attendance.upsertEmployee(uid, "Personel"), Result::Ok);
  }
  CHECK(attendance.count() == 32);
  CHECK_RESULT(scan(attendance, "A1B20000", 1000), Result::Ok);
  CHECK(attendance.lastSequence() == 1);

  makeUid(uid, 32);
  CHECK_RESULT(attendance.addEmployee(uid, "Yeni", uid), Result::Full);
  CHECK(attendance.count() == 32);

  makeUid(uid, 5);
  CHECK_RESULT(attendance.removeEmployee(uid), Result::Ok);
  CHECK(attendance.count() == 31);
  CHECK(attendance.find(uid) == nullptr);
  CHECK(attendance.at(31) == nullptr);

  makeUid(uid, 6);
  CHECK(attendance.at(5) != nullptr);
  CHECK(strcmp(attendance.at(5)->uid, uid) == 0);
  CHECK(attendance.lastSequence() == 1);
  CHECK(attendance.lastEpoch() == 1000);
  CHECK(attendance.find("A1B20000")->inside);
  CHECK(attendance.find("A1B20000")->entryEpoch == 1000);

  CHECK_RESULT(attendance.addEmployee("A1B2FFFF", "Yeni", "A1B2FFFE"),
               Result::Ok);
  CHECK(attendance.count() == 32);
  CHECK(attendance.at(31) != nullptr);
  CHECK(strcmp(attendance.at(31)->uid, "A1B2FFFF") == 0);
  CHECK(attendance.find("A1B2FFFF") != nullptr);

  // Removing the very last entry also clears the tail.
  CHECK_RESULT(attendance.removeEmployee("A1B2FFFF"), Result::Ok);
  CHECK(attendance.count() == 31);
  CHECK(attendance.at(30) != nullptr);
  CHECK(attendance.at(31) == nullptr);
}

int main() {
  testEventShapeAndPreviewSnapshot();
  testLegacyRowsWithoutName();
  testRenameKeepsEventSnapshots();
  testCommitNameValidation();
  testRecoveryNeverCarriesName();
  testManualSnapshotAndRename();
  testRemoveEmployeeRules();
  testAddEmployeeContract();
  testRemovedCardReissuedNewIdentity();
  testArchivedHistoryAfterRemoval();
  testArchivedValidation();
  testRemovalFreesSlotAndKeepsState();

  std::printf("%d checks, %d failure(s)\n", g_checks, g_failures);
  return g_failures == 0 ? 0 : 1;
}
