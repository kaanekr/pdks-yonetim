// Native tests for the SD mirror contract of the named/archived generation
// rewrite in src/Storage.cpp (PDKS_ENABLE_SD=1 + PDKS_SD_SOFTSPI=1).
// Real Storage.cpp/Attendance.cpp are compiled; only pdks::softsd and the
// LittleFS/Arduino APIs are fake. No device, serial, network or real SD.
// Build (host): tools/test_named_sd.py (-DPDKS_REWRITE_FAULTS).
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "Arduino.h"
#include "LittleFS.h"

// The production file calls these in beginSd(); the shared Arduino mock does
// not declare them, so the test provides inert definitions.
#define LOW 0
#define HIGH 1
#define OUTPUT 1
inline void digitalWrite(uint8_t, uint8_t) {}
inline void pinMode(uint8_t, uint8_t) {}

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

// Fake microSD card: a separate flat file map with its own event log.
namespace fakesd {
std::map<std::string, std::string> files;
bool beginOk = true;
bool failAppend = false;
bool failReset = false;
const char* EVENTS = "/SD/events.log";
}  // namespace fakesd

MockFS LittleFS;
#include "../src/Storage.cpp"

// Definitions for the (otherwise unlinked) soft-SPI SD driver. Every mutation
// passes through storemock::operation() so power cuts are injected evenly.
namespace pdks {
namespace softsd {
bool begin() { return fakesd::beginOk; }
bool appendLine(const char* data, size_t length) {
  if (fakesd::failAppend) return false;
#ifdef PDKS_REWRITE_FAULTS
  storemock::operation();
#endif
  std::string& log = fakesd::files[fakesd::EVENTS];
  log.append(data, length);
#ifdef PDKS_REWRITE_FAULTS
  storemock::operation();  // torn line: body written, newline pending
#endif
  log.push_back('\n');
  return true;
}
bool resetEvents() {
  if (fakesd::failReset) return false;
#ifdef PDKS_REWRITE_FAULTS
  storemock::operation();
#endif
  fakesd::files.erase(fakesd::EVENTS);
  return true;
}
}  // namespace softsd
}  // namespace pdks

using namespace pdks;

static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; std::printf("FAIL %d: %s\n", __LINE__, #x); } } while (0)
#define CHECK_RESULT(x, e) CHECK((x) == (e))

static const uint32_t T = 1800000000UL;
static const char* const SD_RECEIPT = "/attendance.sd";

// ---------------------------------------------------------------- helpers
static void resetStorageGlobals() {
  storemock::failWrite = storemock::failRead = storemock::failRename = false;
  fakesd::beginOk = true;
  fakesd::failAppend = false;
  fakesd::failReset = false;
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
  storage::g_sdOk = false;
  storage::g_revision = 0;
  storage::g_pendingCleanup = false;
  storage::g_rosterPath = storage::EMPLOYEES_PATH;
  storage::g_journalPath = storage::JOURNAL_PATH;
}

static bool replayEvent(const Event& event, void* core) {
  return static_cast<Attendance*>(core)->commit(event) == Result::Ok;
}

// Full device boot: mount, roster, journal replay, SD mount (which finishes a
// pending rewrite), then the explicit cleanup call the firmware makes.
static storage::Status deviceBoot(Attendance& core) {
  resetStorageGlobals();
  if (!storage::mount()) return storage::Status::IoError;
  storage::Status status = storage::loadEmployees(core);
  if (status != storage::Status::Ok) return status;
  status = storage::replayJournal(replayEvent, &core);
  if (status != storage::Status::Ok) return status;
  if (storage::sdEnabled() && !storage::beginSd()) return storage::Status::IoError;
  return storage::completePendingRewrite();
}

static void putSentinels() {
  storemock::files["/wifi.json"] = "wifi-sentinel";
  storemock::files["/index.html"] = "panel-sentinel";
  storemock::files["/extra.txt"] = "keep-me";
  fakesd::files["/SD/keep.dat"] = "sd-sentinel";
  fakesd::files["/SD/another.log"] = "sd-log-sentinel";
}

static void checkSentinels() {
  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");
  CHECK(storemock::files["/index.html"] == "panel-sentinel");
  CHECK(storemock::files["/extra.txt"] == "keep-me");
  CHECK(fakesd::files["/SD/keep.dat"] == "sd-sentinel");
  CHECK(fakesd::files["/SD/another.log"] == "sd-log-sentinel");
  CHECK(storemock::formats == 0);  // never formats, never touches other files
}

static std::string sdRaw() {
  std::map<std::string, std::string>::const_iterator it = fakesd::files.find(fakesd::EVENTS);
  return it == fakesd::files.end() ? std::string() : it->second;
}

static std::string canonical() {
  std::map<std::string, std::string>::const_iterator it =
      storemock::files.find(storage::g_journalPath);
  return it == storemock::files.end() ? std::string() : it->second;
}

static std::vector<Event> journalEvents() {
  std::vector<Event> events;
  CHECK_RESULT(storage::replayJournal(
                   [](const Event& event, void* raw) {
                     static_cast<std::vector<Event>*>(raw)->push_back(event);
                     return true;
                   },
                   &events),
               storage::Status::Ok);
  return events;
}

static void appendScan(Attendance& core, const char* uid, uint32_t epoch) {
  Event event;
  CHECK_RESULT(core.preview(uid, epoch, event), Result::Ok);
  CHECK_RESULT(storage::appendEvent(event), storage::Status::Ok);
  CHECK_RESULT(core.commit(event), Result::Ok);
}

// Fresh device: blank LittleFS + blank SD, roster with Ali/Veli and one closed
// session per person (sequences 1-4), SD mirror in sync with the journal.
static void seedMain(Attendance& core) {
  storemock::files.clear();
  fakesd::files.clear();
  putSentinels();
  resetStorageGlobals();
  CHECK(storage::mount());
  CHECK_RESULT(storage::loadEmployees(core), storage::Status::Ok);
  CHECK_RESULT(storage::replayJournal(replayEvent, &core), storage::Status::Ok);
  CHECK(storage::beginSd());
  CHECK_RESULT(storage::completePendingRewrite(), storage::Status::Ok);
  CHECK_RESULT(core.addEmployee("11223344", "Ali", "11223344"), Result::Ok);
  CHECK_RESULT(core.addEmployee("11223355", "Veli", "11223355"), Result::Ok);
  CHECK_RESULT(storage::saveEmployees(core, nullptr), storage::Status::Ok);
  appendScan(core, "11223344", T);
  appendScan(core, "11223344", T + 100);
  appendScan(core, "11223355", T + 200);
  appendScan(core, "11223355", T + 260);
  CHECK(core.lastSequence() == 4);
  CHECK(core.find("11223344")->totalSeconds == 100);
  CHECK(core.find("11223355")->totalSeconds == 60);
  CHECK(sdRaw() == canonical());
  CHECK(sdRaw().find("11223344") != std::string::npos);
  CHECK(storemock::files.count(SD_RECEIPT) == 0);  // no rewrite yet
  checkSentinels();
}

// ------------------------------------------------------------- scenarios
// A deleted I/O pair disappears from the raw SD mirror too; surviving rows keep
// their name snapshots and the mirror stays byte-identical to LittleFS.
static void testPairDeleteMirrorsSdRaw() {
  Attendance core;
  seedMain(core);
  const std::string beforeSd = sdRaw();
  CHECK_RESULT(storage::deleteSession(core, 1), storage::Status::Ok);
  CHECK(core.count() == 2);
  CHECK(core.find("11223344")->totalSeconds == 0);
  CHECK(core.find("11223355")->totalSeconds == 60);
  CHECK(storage::journalRevision() == 1);
  CHECK(!storemock::files.count("/journal.log"));
  const std::string& log = storemock::files["/attendance-b.log"];
  CHECK(log.find("11223344") == std::string::npos);  // deleted history is gone
  // Raw mirror: deleted history is gone, surviving names are still encoded.
  const std::string sd = sdRaw();
  CHECK(sd != beforeSd);
  CHECK(sd == canonical());                 // mirror equals the new generation
  CHECK(sd.find("11223344,I,0") == std::string::npos);
  CHECK(sd.find("11223344,O,0") == std::string::npos);
  CHECK(sd.find("56656C69") != std::string::npos);  // hex("Veli")
  CHECK(storemock::files[SD_RECEIPT] == "1");
  std::vector<Event> events = journalEvents();
  CHECK(events.size() == 2);
  CHECK(strcmp(events[0].uid, "11223355") == 0);
  CHECK(strcmp(events[0].name, "Veli") == 0);
  CHECK(!events[0].archived);
  // Deleted employee still exists in the roster, but has no history left.
  bool found = true;
  CHECK_RESULT(storage::uidHasHistory("11223344", found), storage::Status::Ok);
  CHECK(!found);
  Attendance rebooted;
  CHECK_RESULT(deviceBoot(rebooted), storage::Status::Ok);
  CHECK(rebooted.find("11223344")->totalSeconds == 0);
  CHECK(rebooted.find("11223355")->totalSeconds == 60);
  CHECK(sdRaw() == canonical());  // receipt matched: no needless SD rewrite
  checkSentinels();
}

// Deleting a person archives their history in the new generation, and the SD
// mirror carries the archived flag plus the preserved name snapshot.
static void testPersonDeleteArchivesSdNames() {
  Attendance core;
  seedMain(core);
  CHECK_RESULT(storage::deleteEmployee(core, "11223355"), storage::Status::Ok);
  CHECK(core.count() == 1);
  CHECK(core.find("11223355") == nullptr);
  CHECK(core.find("11223344")->totalSeconds == 100);
  CHECK(storage::journalRevision() == 1);
  const std::string& log = storemock::files["/attendance-b.log"];
  CHECK(log.find("56656C69,1,1") != std::string::npos);  // Veli, recorded, archived
  CHECK(log.find("416C69,1,0") != std::string::npos);    // Ali, recorded, live
  const std::string sd = sdRaw();
  CHECK(sd == canonical());
  CHECK(sd.find("56656C69,1,1") != std::string::npos);
  CHECK(storemock::files[SD_RECEIPT] == "1");
  Attendance rebooted;
  CHECK_RESULT(deviceBoot(rebooted), storage::Status::Ok);
  std::vector<Event> events = journalEvents();
  CHECK(events.size() == 4);  // Ali's two live rows, then Veli's archived pair
  CHECK(strcmp(events[0].uid, "11223344") == 0);
  CHECK(!events[0].archived);
  CHECK(strcmp(events[2].name, "Veli") == 0);
  CHECK(events[2].archived);
  CHECK(strcmp(events[3].uid, "11223355") == 0);
  CHECK(events[3].archived);
  bool history = false;
  CHECK_RESULT(storage::uidHasHistory("11223355", history), storage::Status::Ok);
  CHECK(history);
  CHECK(rebooted.count() == 1);
  checkSentinels();
}

// With SD enabled the rewrite preflight refuses to start unless the mirror is
// mounted; nothing at all may change (no selector, no files, no lock).
static void testPreflightWithoutSdBlocks() {
  Attendance core;
  seedMain(core);
  const std::map<std::string, std::string> beforeFs = storemock::files;
  const std::map<std::string, std::string> beforeSd = fakesd::files;
  fakesd::beginOk = false;
  CHECK(!storage::beginSd());
  CHECK(!storage::sdOk());
  CHECK_RESULT(storage::deleteEmployee(core, "11223355"), storage::Status::BadData);
  CHECK_RESULT(storage::deleteSession(core, 1), storage::Status::BadData);
  CHECK(storemock::files == beforeFs);
  CHECK(fakesd::files == beforeSd);
  CHECK(storage::journalRevision() == 0);
  CHECK(!storage::locked());
  CHECK(core.count() == 2);
  fakesd::beginOk = true;
  CHECK(storage::beginSd());
  CHECK_RESULT(storage::deleteEmployee(core, "11223355"), storage::Status::Ok);
  CHECK(storage::journalRevision() == 1);
  checkSentinels();
}

// The selector commit is one atomic step, but the SD receipt/reset/mirror and
// the old-file cleanup come after it. A failure there must NOT report success
// or leave the device usable with a half-mirrored SD; the next boot (mount +
// replay + beginSd) finishes the pending rewrite exactly once.
static void testSdFailureLocksNewGenerationThenRebootRepairs() {
  // (a) resetEvents fails before the mirror is rebuilt.
  Attendance core;
  seedMain(core);
  const std::map<std::string, std::string> oldSd = fakesd::files;
  fakesd::failReset = true;
  CHECK_RESULT(storage::deleteEmployee(core, "11223355"), storage::Status::IoError);
  CHECK(storage::locked());
  CHECK(!storage::sdOk());  // a failed reset must never advertise a healthy SD
  CHECK(storage::journalRevision() == 1);
  CHECK(storemock::files.count("/attendance.current") == 1);
  CHECK(!storemock::files.count(SD_RECEIPT));
  CHECK(storemock::files.count("/journal.log") == 1);  // cleanup not reached
  CHECK(storemock::files.count("/employees.json") == 1);
  CHECK(fakesd::files == oldSd);  // SD untouched, and no false success
  fakesd::failReset = false;
  Attendance repaired;
  CHECK_RESULT(deviceBoot(repaired), storage::Status::Ok);
  CHECK(!storage::locked());
  CHECK(storage::sdOk());
  CHECK(storage::journalRevision() == 1);
  CHECK(repaired.count() == 1);
  CHECK(repaired.find("11223355") == nullptr);  // no resurrection
  CHECK(sdRaw() == canonical());
  CHECK(storemock::files[SD_RECEIPT] == "1");
  CHECK(!storemock::files.count("/journal.log"));
  CHECK(!storemock::files.count("/employees.json"));

  // (b) the mirror append fails after the events were reset.
  Attendance other;
  seedMain(other);
  fakesd::failAppend = true;
  CHECK_RESULT(storage::deleteSession(other, 1), storage::Status::IoError);
  CHECK(storage::locked());
  CHECK(!storage::sdOk());
  CHECK(storage::journalRevision() == 1);
  CHECK(!storemock::files.count(SD_RECEIPT));
  CHECK(!fakesd::files.count(fakesd::EVENTS));  // reset succeeded, mirror empty
  CHECK(other.find("11223355")->totalSeconds == 60);
  fakesd::failAppend = false;
  Attendance other2;
  CHECK_RESULT(deviceBoot(other2), storage::Status::Ok);
  CHECK(!storage::locked());
  CHECK(other2.find("11223344")->totalSeconds == 0);
  CHECK(other2.find("11223355")->totalSeconds == 60);
  CHECK(canonical().find("11223344") == std::string::npos);
  CHECK(sdRaw() == canonical());
  CHECK(storemock::files[SD_RECEIPT] == "1");
  checkSentinels();
}

// The durable dirty bit repairs a transient normal-operation append gap on boot.
static void testTransientSdAppendFailureRepairsMirrorGap() {
  Attendance core;
  seedMain(core);
  fakesd::failAppend = true;
  appendScan(core, "11223344", T + 400);  // journal ok, mirror fails
  CHECK(!storage::sdOk());
  CHECK(sdRaw() != canonical());
  fakesd::failAppend = false;
  Attendance rebooted;
  CHECK_RESULT(deviceBoot(rebooted), storage::Status::Ok);
  CHECK(storage::sdOk());
  CHECK(rebooted.lastSequence() == 5);
  CHECK(sdRaw() == canonical());
  CHECK(sdRaw().find("11223344,I,0") != std::string::npos);
  // The next committed rewrite does resynchronise the whole mirror.
  CHECK_RESULT(storage::deleteSession(rebooted, 1), storage::Status::Ok);
  CHECK(sdRaw() == canonical());
  CHECK(storemock::files[SD_RECEIPT] == "1");
  checkSentinels();
}

#ifdef PDKS_REWRITE_FAULTS
static bool coreIsOld(const Attendance& core) {
  const Employee* a = core.find("11223344");
  const Employee* b = core.find("11223355");
  return core.count() == 2 && a && b && !a->inside && a->totalSeconds == 100 &&
         b->totalSeconds == 60 && core.lastSequence() == 4;
}

static bool coreIsNew(const Attendance& core) {
  const Employee* a = core.find("11223344");
  const Employee* b = core.find("11223355");
  return core.count() == 2 && a && b && !a->inside && a->totalSeconds == 0 &&
         b->totalSeconds == 60 && core.lastSequence() == 2;
}

// Rebuild the exact pre-deletion device state without performing a delete.
static void restoreBaseline(const std::map<std::string, std::string>& fs,
                            const std::map<std::string, std::string>& sd,
                            Attendance& core) {
  storemock::files = fs;
  fakesd::files = sd;
  resetStorageGlobals();
  CHECK(storage::mount());
  CHECK_RESULT(storage::loadEmployees(core), storage::Status::Ok);
  CHECK_RESULT(storage::replayJournal(replayEvent, &core), storage::Status::Ok);
  CHECK(storage::beginSd());
  CHECK_RESULT(storage::completePendingRewrite(), storage::Status::Ok);
}

static void testPowerCutEveryBoundary() {
  Attendance initial;
  seedMain(initial);
  const std::map<std::string, std::string> baselineFs = storemock::files;
  const std::map<std::string, std::string> baselineSd = fakesd::files;
  const std::string oldJournal = canonical();

  // Dry run: count every mock operation (LittleFS + fake SD) one delete touches.
  Attendance dry;
  restoreBaseline(baselineFs, baselineSd, dry);
  storemock::countingBegin();
  CHECK_RESULT(storage::deleteSession(dry, 1), storage::Status::Ok);
  const int total = storemock::operations();
  storemock::disableCut();
  CHECK(total > 10);

  int sawOld = 0, sawNew = 0;
  for (int cut = 1; cut <= total; ++cut) {
    Attendance attempt;
    restoreBaseline(baselineFs, baselineSd, attempt);
    storemock::armCut(cut);
    bool threw = false;
    try {
      (void)storage::deleteSession(attempt, 1);
    } catch (const storemock::PowerCut&) {
      threw = true;
    }
    storemock::disableCut();
    CHECK(threw);

    // Reboot: the selector decides the generation, beginSd completes the
    // pending SD reset/mirror and cleanup, exactly like the firmware.
    Attendance after;
    CHECK_RESULT(deviceBoot(after), storage::Status::Ok);
    const uint32_t revision = storage::journalRevision();
    CHECK(revision == 0 || revision == 1);
    if (revision == 0) {
      ++sawOld;
      CHECK(coreIsOld(after));
      CHECK(storemock::files.count("/journal.log") == 1);
      CHECK(storemock::files.count("/employees.json") == 1);
      CHECK(!storemock::files.count("/attendance.current"));
      CHECK(!storemock::files.count(SD_RECEIPT));
      CHECK(sdRaw() == oldJournal);
      bool history = false;
      CHECK_RESULT(storage::uidHasHistory("11223344", history), storage::Status::Ok);
      CHECK(history);
    } else {
      ++sawNew;
      CHECK(coreIsNew(after));
      CHECK(!storemock::files.count("/journal.log"));
      CHECK(!storemock::files.count("/employees.json"));
      CHECK(!storemock::files.count("/attendance-a.log"));
      CHECK(!storemock::files.count("/attendance-a.json"));
      CHECK(storemock::files.count("/attendance-b.log") == 1);
      CHECK(storemock::files.count("/attendance-b.json") == 1);
      CHECK(storemock::files.count("/attendance.current") == 1);
      CHECK(storemock::files[SD_RECEIPT] == "1");
      CHECK(sdRaw() == canonical());
      CHECK(sdRaw().find("11223344") == std::string::npos);
      bool history = true;
      CHECK_RESULT(storage::uidHasHistory("11223344", history), storage::Status::Ok);
      CHECK(!history);
      // A committed deletion never comes back on a later boot.
      Attendance second;
      CHECK_RESULT(deviceBoot(second), storage::Status::Ok);
      CHECK(storage::journalRevision() == 1);
      CHECK(coreIsNew(second));
      CHECK(sdRaw() == canonical());
    }
    checkSentinels();
  }
  CHECK(sawOld > 0);
  CHECK(sawNew > 0);
  std::printf("SD power-cut boundaries: %d (%d old, %d new)\n", total, sawOld, sawNew);
}
#endif

int main() {
  testPairDeleteMirrorsSdRaw();
  testPersonDeleteArchivesSdNames();
  testPreflightWithoutSdBlocks();
  testSdFailureLocksNewGenerationThenRebootRepairs();
  testTransientSdAppendFailureRepairsMirrorGap();
#ifdef PDKS_REWRITE_FAULTS
  testPowerCutEveryBoundary();
#endif
  std::printf("named sd: %d checks, %d failure(s)\n", checks, failures);
  return failures ? 1 : 0;
}
