// Native fault-injection tests for named/archived journal persistence and the
// dual-generation rewrite path in src/Storage.cpp. No device, no Arduino.
// Build (host): see tools/test_named_storage.py (-DPDKS_REWRITE_FAULTS).
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "LittleFS.h"

namespace storemock {
std::map<std::string, std::string> files;
bool failWrite = false, failRead = false, failRename = false;
int formats = 0;
#ifdef PDKS_REWRITE_FAULTS
int g_operations = 0, g_cut = 0;
bool g_cutOn = false;
struct PowerCut {};
void operation() {
  if (!g_cutOn) return;
  if (++g_operations == g_cut) throw PowerCut();
}
void countingBegin() { g_operations = 0; g_cut = 0; g_cutOn = true; }
void armCut(int at) { g_operations = 0; g_cut = at; g_cutOn = true; }
void disableCut() { g_cutOn = false; g_cut = 0; }
int operations() { return g_operations; }
#endif
}  // namespace storemock
MockFS LittleFS;
#include "../src/Storage.cpp"

using namespace pdks;

static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; std::printf("FAIL %d: %s\n", __LINE__, #x); } } while (0)
#define CHECK_RESULT(x, e) CHECK((x) == (e))

static const uint32_t T = 1800000000UL;

// ---------------------------------------------------------------- helpers
static void resetStorageGlobals() {
  storemock::failWrite = storemock::failRead = storemock::failRename = false;
#ifdef PDKS_REWRITE_FAULTS
  storemock::disableCut();
#endif
  storage::g_mounted = false;
  storage::g_locked = false;
  storage::g_ready = false;
  storage::g_haveSeq = false;
  storage::g_lastSeq = 0;
  storage::g_lastEpoch = 0;
  storage::g_lines = 0;
}

static void boot() {
  resetStorageGlobals();
  CHECK(storage::mount());
}

static bool replayEvent(const Event& event, void* core) {
  return static_cast<Attendance*>(core)->commit(event) == Result::Ok;
}

static bool collectEvent(const Event& event, void* raw) {
  static_cast<std::vector<Event>*>(raw)->push_back(event);
  return true;
}

// Full device boot: mount, roster, journal replay, deferred rewrite cleanup.
static storage::Status deviceBoot(Attendance& core) {
  boot();
  storage::Status status = storage::loadEmployees(core);
  if (status != storage::Status::Ok) return status;
  status = storage::replayJournal(replayEvent, &core);
  if (status != storage::Status::Ok) return status;
  return storage::completePendingRewrite();
}

static std::vector<Event> journalEvents() {
  std::vector<Event> events;
  const storage::Status status = storage::replayJournal(collectEvent, &events);
  CHECK_RESULT(status, storage::Status::Ok);
  return events;
}

static void putSentinels() {
  storemock::files["/wifi.json"] = "wifi-sentinel";
  storemock::files["/index.html"] = "panel-sentinel";
  storemock::files["/extra.txt"] = "keep-me";
}

static void checkSentinels() {
  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");
  CHECK(storemock::files["/index.html"] == "panel-sentinel");
  CHECK(storemock::files["/extra.txt"] == "keep-me");
  CHECK(storemock::formats == 0);  // no implicit format on any path
}

static void appendScan(Attendance& core, const char* uid, uint32_t epoch) {
  Event event;
  CHECK_RESULT(core.preview(uid, epoch, event), Result::Ok);
  CHECK_RESULT(storage::appendEvent(event), storage::Status::Ok);
  CHECK_RESULT(core.commit(event), Result::Ok);
}

static void appendManual(Attendance& core, const char* uid, uint32_t start, uint32_t end) {
  Event event;
  CHECK_RESULT(core.previewManual(uid, start, end, event), Result::Ok);
  CHECK_RESULT(storage::appendEvent(event), storage::Status::Ok);
  CHECK_RESULT(core.commit(event), Result::Ok);
}

static void appendRecovery(Attendance& core, uint32_t epoch) {
  Event event;
  CHECK_RESULT(core.previewRecovery(epoch, event), Result::Ok);
  CHECK_RESULT(storage::appendEvent(event), storage::Status::Ok);
  CHECK_RESULT(core.commit(event), Result::Ok);
}

static void seedDevice(Attendance& core) {
  storemock::files.clear();
  putSentinels();
  boot();
  Attendance roster;
  CHECK_RESULT(roster.addEmployee("11223344", "Ali", "11223344"), Result::Ok);
  CHECK_RESULT(roster.addEmployee("11223355", "Veli", "11223355"), Result::Ok);
  CHECK_RESULT(storage::saveEmployees(roster, nullptr), storage::Status::Ok);
  CHECK_RESULT(storage::loadEmployees(core), storage::Status::Ok);
  CHECK_RESULT(storage::replayJournal(replayEvent, &core), storage::Status::Ok);
}

// Shared fixture:
//   1 A I T        2 A O T+100       3 R T+150 (baseline T+100)
//   4 B I T+200    5 B O T+260       6 A M 600       7 A I T+1000 (open)
static void buildMain(Attendance& core) {
  seedDevice(core);
  appendScan(core, "11223344", T);
  appendScan(core, "11223344", T + 100);
  appendRecovery(core, T + 150);
  appendScan(core, "11223355", T + 200);
  appendScan(core, "11223355", T + 260);
  appendManual(core, "11223344", T + 300, T + 900);
  appendScan(core, "11223344", T + 1000);
  CHECK(core.find("11223344")->totalSeconds == 700);
  CHECK(core.find("11223355")->totalSeconds == 60);
  CHECK(core.find("11223344")->inside);
  CHECK(core.lastSequence() == 7);
}

// ------------------------------------------------------------- scenarios
static void testNamedCodecRoundTrip() {
  storemock::files.clear();
  putSentinels();
  boot();
  Attendance seed;
  const char* quoted = "Ay\xC5\x9F" "e \"Su\" \\ Test";
  char maximum[49];
  memset(maximum, 'A', 48);
  maximum[48] = '\0';
  CHECK_RESULT(seed.addEmployee("11223344", quoted, "11223344"), Result::Ok);
  CHECK_RESULT(seed.addEmployee("11223355", maximum, "11223355"), Result::Ok);
  CHECK_RESULT(storage::saveEmployees(seed, nullptr), storage::Status::Ok);
  Attendance core;
  CHECK_RESULT(storage::loadEmployees(core), storage::Status::Ok);
  CHECK(core.find("11223344") != nullptr);
  CHECK(strcmp(core.find("11223344")->name, quoted) == 0);
  CHECK(strcmp(core.find("11223355")->name, maximum) == 0);
  CHECK_RESULT(storage::replayJournal(replayEvent, &core), storage::Status::Ok);
  appendScan(core, "11223344", T);
  appendScan(core, "11223344", T + 100);
  const std::string& log = storemock::files["/journal.log"];
  CHECK(log.find("4,1,") != std::string::npos);  // named record marker
  CHECK(log.find("Ay") == std::string::npos);    // name is hex encoded
  CHECK(log.find("\"") == std::string::npos);    // no raw quote in the line
  Attendance restored;
  CHECK_RESULT(storage::loadEmployees(restored), storage::Status::Ok);
  CHECK_RESULT(storage::replayJournal(replayEvent, &restored), storage::Status::Ok);
  std::vector<Event> events = journalEvents();
  CHECK(events.size() == 2);
  CHECK(strcmp(events[0].name, quoted) == 0);
  CHECK(events[0].nameRecorded);
  CHECK(!events[0].archived);
  CHECK(events[1].type == 'O' && events[1].seconds == 100);
  // Any body tamper is caught by the CRC and fails closed.
  const std::string good = storemock::files["/journal.log"];
  storemock::files["/journal.log"][0] = '2';
  Attendance broken;
  CHECK_RESULT(deviceBoot(broken), storage::Status::IntegrityError);
  CHECK(storage::locked());
  storemock::files["/journal.log"] = good;
  Attendance fixed;
  CHECK_RESULT(deviceBoot(fixed), storage::Status::Ok);
  CHECK(!storage::locked());
  checkSentinels();
}

static void testLegacyJournalMigration() {
  storemock::files.clear();
  putSentinels();
  storemock::files["/employees.json"] =
      "{\"v\":1,\"employees\":[{\"uid\":\"11223344\",\"name\":\"Ali\"}]}";
  Event in;
  in.sequence = 1;
  in.epoch = T;
  memcpy(in.uid, "11223344", 9);
  in.type = 'I';
  Event out;
  out.sequence = 2;
  out.epoch = T + 100;
  memcpy(out.uid, "11223344", 9);
  out.type = 'O';
  out.seconds = 100;
  Event manual;
  manual.sequence = 3;
  manual.epoch = T + 700;
  memcpy(manual.uid, "11223344", 9);
  manual.type = 'M';
  manual.seconds = 500;
  std::string log;
  for (Event* event : {&in, &out, &manual}) {
    char line[storage::JOURNAL_MAX_LINE];
    const int length = storage::buildLine(line, sizeof(line), *event);
    CHECK(length > 0);
    log.append(line, static_cast<size_t>(length));
    log += '\n';
  }
  storemock::files["/journal.log"] = log;
  CHECK(log.compare(0, 2, "4,") != 0);  // legacy version marker, not a substring of a UID
  Attendance core;
  CHECK_RESULT(deviceBoot(core), storage::Status::Ok);
  CHECK(core.find("11223344") != nullptr);
  CHECK(core.find("11223344")->totalSeconds == 600);
  CHECK_RESULT(storage::deleteSession(core, 3), storage::Status::Ok);
  CHECK(core.find("11223344")->totalSeconds == 100);
  CHECK(storemock::files.count("/attendance-b.log") == 1);
  const std::string& migrated = storemock::files["/attendance-b.log"];
  CHECK(migrated.find("4,1,") != std::string::npos);
  CHECK(migrated.find("4,2,") != std::string::npos);
  CHECK(!storemock::files.count("/journal.log"));
  Attendance restored;
  CHECK_RESULT(deviceBoot(restored), storage::Status::Ok);
  CHECK(restored.find("11223344")->totalSeconds == 100);
  CHECK(!restored.find("11223344")->inside);
  std::vector<Event> events = journalEvents();
  CHECK(events.size() == 2);
  CHECK(strcmp(events[0].name, "Ali") == 0);
  CHECK(strcmp(events[1].name, "Ali") == 0);
  CHECK(!events[0].nameRecorded);  // inferred migration, not a live snapshot
  CHECK(!events[1].nameRecorded);
  CHECK(!events[0].archived);
  checkSentinels();
}

static void testDeleteEmployeeArchivesHistory() {
  Attendance core;
  buildMain(core);
  const std::string oldLog = storemock::files["/journal.log"];
  CHECK(oldLog.find("11223344,I,0") != std::string::npos);
  CHECK_RESULT(storage::deleteEmployee(core, "11223355"), storage::Status::Ok);
  CHECK(core.count() == 1);
  CHECK(core.find("11223355") == nullptr);
  CHECK(core.find("11223344")->totalSeconds == 700);
  CHECK(core.find("11223344")->inside);
  CHECK(storage::journalRevision() == 1);
  CHECK(!storemock::files.count("/journal.log"));  // old generation removed
  CHECK(!storemock::files.count("/employees.json"));
  CHECK(storemock::files.count("/attendance.current") == 1);
  bool found = false;
  CHECK_RESULT(storage::uidHasHistory("11223355", found), storage::Status::Ok);
  CHECK(found);  // historic attendance is retained
  std::vector<Event> events = journalEvents();
  size_t archived = 0;
  for (const Event& event : events) {
    if (strcmp(event.uid, "11223355") != 0) continue;
    CHECK(event.archived);
    CHECK(event.nameRecorded);
    CHECK(strcmp(event.name, "Veli") == 0);
    ++archived;
  }
  CHECK(archived == 2);
  CHECK(storemock::files.count("/attendance-b.json") == 1);
  const std::string& roster = storemock::files["/attendance-b.json"];
  CHECK(roster.find("11223355") == std::string::npos);
  CHECK(roster.find("Veli") == std::string::npos);
  // A survivor rename never rewrites archived history.
  CHECK_RESULT(core.upsertEmployee("11223344", "Ali Yeni"), Result::Ok);
  CHECK_RESULT(storage::saveEmployees(core, nullptr), storage::Status::Ok);
  std::vector<Event> after = journalEvents();
  CHECK(after.size() == 7);
  CHECK(strcmp(after[3].name, "Veli") == 0);
  CHECK(after[3].archived);
  // Reboot reads the pointer and the archived generation.
  Attendance rebooted;
  CHECK_RESULT(deviceBoot(rebooted), storage::Status::Ok);
  CHECK(storage::journalRevision() == 1);
  CHECK(rebooted.count() == 1);
  CHECK(strcmp(rebooted.find("11223344")->name, "Ali Yeni") == 0);
  CHECK(rebooted.find("11223344")->totalSeconds == 700);
  CHECK(rebooted.anyInside());
  // The freed slot accepts a new identity holding the recycled card.
  CHECK_RESULT(rebooted.addEmployee("11223366", "Ayse", "11223355"), Result::Ok);
  CHECK_RESULT(storage::saveEmployees(rebooted, nullptr), storage::Status::Ok);
  Attendance final;
  CHECK_RESULT(deviceBoot(final), storage::Status::Ok);
  CHECK(final.count() == 2);
  CHECK(final.findByCard("11223355") != nullptr);
  CHECK(strcmp(final.findByCard("11223355")->uid, "11223366") == 0);
  CHECK(final.find("11223355") == nullptr);
  checkSentinels();
}

static void testDeletePairedSessionRecomputes() {
  Attendance core;
  buildMain(core);
  const uint32_t revision = storage::journalRevision();
  CHECK(revision == 0);
  CHECK_RESULT(storage::deleteSession(core, 4), storage::Status::Ok);
  CHECK(storage::journalRevision() == revision + 1);
  CHECK(core.count() == 2);
  CHECK(core.find("11223355")->totalSeconds == 0);
  CHECK(core.find("11223344")->totalSeconds == 700);
  CHECK(core.find("11223344")->inside);
  std::vector<Event> events = journalEvents();
  CHECK(events.size() == 5);
  for (size_t i = 0; i < events.size(); ++i) {
    CHECK(events[i].sequence == i + 1);  // resequenced, no gaps
  }
  CHECK(events[2].type == 'R');
  CHECK(events[2].seconds == T + 100);  // recovered baseline of surviving rows
  CHECK(!events[2].nameRecorded);
  bool found = true;
  CHECK_RESULT(storage::uidHasHistory("11223355", found), storage::Status::Ok);
  CHECK(!found);
  Attendance rebooted;
  CHECK_RESULT(deviceBoot(rebooted), storage::Status::Ok);
  CHECK(rebooted.find("11223355")->totalSeconds == 0);
  CHECK(rebooted.find("11223344")->totalSeconds == 700);
  CHECK(rebooted.find("11223344")->inside);
  CHECK(rebooted.lastSequence() == 5);
  checkSentinels();
}

static void testDeleteManualRecomputesTotals() {
  Attendance core;
  buildMain(core);
  CHECK_RESULT(storage::deleteSession(core, 6), storage::Status::Ok);
  CHECK(core.find("11223344")->totalSeconds == 100);
  CHECK(core.find("11223344")->inside);
  std::vector<Event> events = journalEvents();
  CHECK(events.size() == 6);
  for (const Event& event : events) CHECK(event.type != 'M');
  Attendance rebooted;
  CHECK_RESULT(deviceBoot(rebooted), storage::Status::Ok);
  CHECK(rebooted.find("11223344")->totalSeconds == 100);
  CHECK(rebooted.find("11223344")->inside);
  checkSentinels();
}

static void testDeletePairRewritesRecoveryBaseline() {
  Attendance core;
  buildMain(core);
  CHECK_RESULT(storage::deleteSession(core, 1), storage::Status::Ok);
  std::vector<Event> events = journalEvents();
  CHECK(events.size() == 5);
  CHECK(events[0].type == 'R');  // the first surviving row
  CHECK(events[0].seconds == 0);  // nothing precedes it any more
  CHECK(events[0].epoch == T + 150);
  CHECK(core.find("11223344")->totalSeconds == 600);
  CHECK(core.find("11223344")->inside);
  Attendance rebooted;
  CHECK_RESULT(deviceBoot(rebooted), storage::Status::Ok);
  CHECK(rebooted.lastEpoch() == T + 1000);
  CHECK(rebooted.find("11223344")->totalSeconds == 600);
  checkSentinels();
}

static void testForbiddenDeletesLeaveState() {
  Attendance core;
  buildMain(core);
  const std::map<std::string, std::string> before = storemock::files;
  const uint32_t revision = storage::journalRevision();
  CHECK_RESULT(storage::deleteSession(core, 7), storage::Status::BadData);  // open I
  CHECK_RESULT(storage::deleteSession(core, 3), storage::Status::BadData);  // R row
  CHECK_RESULT(storage::deleteSession(core, 99), storage::Status::BadData);  // missing
  CHECK_RESULT(storage::deleteEmployee(core, "11223344"), storage::Status::BadData);  // open
  CHECK_RESULT(storage::deleteEmployee(core, "FFFFFFFF"), storage::Status::BadData);  // absent
  CHECK_RESULT(storage::deleteEmployee(core, "bad"), storage::Status::BadData);  // invalid
  CHECK(storemock::files == before);
  CHECK(storage::journalRevision() == revision);
  CHECK(core.count() == 2);
  CHECK(core.find("11223344")->totalSeconds == 700);
  CHECK(core.lastSequence() == 7);
  checkSentinels();
}

static void testWriteFailureKeepsGeneration() {
  Attendance core;
  buildMain(core);
  const std::map<std::string, std::string> before = storemock::files;
  const uint32_t revision = storage::journalRevision();
  storemock::failWrite = true;
  CHECK_RESULT(storage::deleteSession(core, 4), storage::Status::IoError);
  CHECK(storemock::files == before);
  CHECK(storage::journalRevision() == revision);
  CHECK(core.find("11223355")->totalSeconds == 60);
  CHECK(core.lastSequence() == 7);
  CHECK(!storage::locked());
  storemock::failWrite = false;
  storemock::failRename = true;
  CHECK_RESULT(storage::deleteSession(core, 4), storage::Status::IoError);
  CHECK(storemock::files == before);
  CHECK(storage::journalRevision() == revision);
  storemock::failRename = false;
  // A failed attempt never blocks the retry.
  CHECK_RESULT(storage::deleteSession(core, 4), storage::Status::Ok);
  const std::string pointer = storemock::files["/attendance.current"];
  const uint32_t committed = storage::journalRevision();
  const std::map<std::string, std::string> good = storemock::files;
  storemock::failWrite = true;
  CHECK_RESULT(storage::deleteSession(core, 4), storage::Status::IoError);  // M row
  CHECK(storemock::files == good);
  CHECK(storemock::files["/attendance.current"] == pointer);
  CHECK(storage::journalRevision() == committed);
  storemock::failWrite = false;
  checkSentinels();
}

#ifdef PDKS_REWRITE_FAULTS
static bool coreIsOld(const Attendance& core) {
  const Employee* a = core.find("11223344");
  const Employee* b = core.find("11223355");
  return core.count() == 2 && a && b && a->inside && a->totalSeconds == 700 &&
         b->totalSeconds == 60 && core.lastSequence() == 7;
}

static bool coreIsNew(const Attendance& core) {
  const Employee* a = core.find("11223344");
  const Employee* b = core.find("11223355");
  return core.count() == 2 && a && b && a->inside && a->totalSeconds == 700 &&
         b->totalSeconds == 0 && !b->inside && core.lastSequence() == 5;
}

// Boot the baseline with cut injection disabled, leaving the device ready to
// perform the deletion exactly like the cut run does.
static Attendance prepareDeletion(const std::map<std::string, std::string>& baseline) {
  storemock::files = baseline;
  resetStorageGlobals();
  CHECK(storage::mount());
  Attendance core;
  CHECK_RESULT(storage::loadEmployees(core), storage::Status::Ok);
  CHECK_RESULT(storage::replayJournal(replayEvent, &core), storage::Status::Ok);
  CHECK_RESULT(storage::completePendingRewrite(), storage::Status::Ok);
  return core;
}

static void testPowerCutEveryBoundary() {
  Attendance initial;
  buildMain(initial);
  const std::map<std::string, std::string> baseline = storemock::files;

  // Dry run counts every mock operation touched by one successful deletion.
  Attendance dry = prepareDeletion(baseline);
  storemock::countingBegin();
  CHECK_RESULT(storage::deleteSession(dry, 4), storage::Status::Ok);
  const int total = storemock::operations();
  storemock::disableCut();
  CHECK(total > 10);

  int sawOld = 0, sawNew = 0;
  for (int cut = 1; cut <= total; ++cut) {
    Attendance attempt = prepareDeletion(baseline);
    storemock::armCut(cut);
    bool threw = false;
    try {
      (void)storage::deleteSession(attempt, 4);
    } catch (const storemock::PowerCut&) {
      threw = true;
    }
    storemock::disableCut();
    CHECK(threw);
    // Reboot: pointer decides the generation, cleanup finishes the rewrite.
    resetStorageGlobals();
    Attendance after;
    CHECK(storage::mount());
    CHECK_RESULT(storage::loadEmployees(after), storage::Status::Ok);
    CHECK_RESULT(storage::replayJournal(replayEvent, &after), storage::Status::Ok);
    CHECK_RESULT(storage::completePendingRewrite(), storage::Status::Ok);
    const uint32_t revision = storage::journalRevision();
    CHECK(revision == 0 || revision == 1);
    if (revision == 0) {
      ++sawOld;
      CHECK(coreIsOld(after));
      CHECK(storemock::files.count("/journal.log") == 1);
      CHECK(storemock::files.count("/employees.json") == 1);
    } else {
      ++sawNew;
      CHECK(coreIsNew(after));
      CHECK(!storemock::files.count("/journal.log"));
      CHECK(!storemock::files.count("/employees.json"));
      CHECK(storemock::files.count("/attendance-b.log") == 1);
      CHECK(storemock::files.count("/attendance-b.json") == 1);
      CHECK(storemock::files.count("/attendance.current") == 1);
    }
    bool history = false;
    CHECK_RESULT(storage::uidHasHistory("11223355", history), storage::Status::Ok);
    CHECK(history == (revision == 0));
    checkSentinels();
  }
  CHECK(sawOld > 0);
  CHECK(sawNew > 0);
  std::printf("power-cut boundaries: %d (%d old, %d new)\n", total, sawOld, sawNew);
}
#endif

int main() {
  testNamedCodecRoundTrip();
  testLegacyJournalMigration();
  testDeleteEmployeeArchivesHistory();
  testDeletePairedSessionRecomputes();
  testDeleteManualRecomputesTotals();
  testDeletePairRewritesRecoveryBaseline();
  testForbiddenDeletesLeaveState();
  testWriteFailureKeepsGeneration();
#ifdef PDKS_REWRITE_FAULTS
  testPowerCutEveryBoundary();
#endif
  std::printf("named storage: %d checks, %d failure(s)\n", checks, failures);
  return failures ? 1 : 0;
}
