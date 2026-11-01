// Focused native tests for additive historical manual attendance (journal type 3
// "M"). The real Attendance.cpp and Storage.cpp run against an in-memory
// LittleFS: no device, network, serial, SD or filesystem access happens.
#include <cstdio>
#include <cstring>
#include <string>

#include "LittleFS.h"

namespace storemock {
std::map<std::string, std::string> files;
bool failWrite = false, failRead = false, failRename = false;
int formats = 0;
}  // namespace storemock

MockFS LittleFS;
#include "../src/Storage.cpp"

using namespace pdks;

static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; std::printf("FAIL %d: %s\n", __LINE__, #x); } } while (0)

static const uint32_t T = 1800000000UL;  // 2027-01-15, inside MIN/MAX_EPOCH
static const char* A = "11223344";
static const char* B = "55AA7788";
static const char* ROSTER_V1 =
    "{\"v\":1,\"employees\":[{\"uid\":\"11223344\",\"name\":\"Ali\"},"
    "{\"uid\":\"55AA7788\",\"name\":\"Veli\"}]}";
static const char* ROSTER_V2 =
    "{\"v\":2,\"employees\":["
    "{\"uid\":\"11223344\",\"name\":\"Ali\",\"card_uid\":\"11223344\",\"active\":true},"
    "{\"uid\":\"55AA7788\",\"name\":\"Veli\",\"card_uid\":\"55AA7788\",\"active\":true}]}";

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// Independent IEEE CRC32 so handcrafted legacy lines never reuse production code.
static uint32_t testCrc(const std::string& body) {
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < body.size(); ++i) {
    crc ^= static_cast<uint8_t>(body[i]);
    for (int bit = 0; bit < 8; ++bit)
      crc = (crc & 1u) ? ((crc >> 1) ^ 0xEDB88320u) : (crc >> 1);
  }
  return ~crc;
}

static std::string legacyLine(const std::string& body) {
  char hex[16];
  std::snprintf(hex, sizeof(hex), "%08lX", (unsigned long)testCrc(body));
  return body + "," + hex + "\n";
}

static std::string manualLine(uint32_t seq, uint32_t epoch, const char* uid,
                              uint32_t seconds) {
  char body[96];
  std::snprintf(body, sizeof(body), "3,%lu,%lu,%s,M,%lu", (unsigned long)seq,
                (unsigned long)epoch, uid, (unsigned long)seconds);
  return legacyLine(body);
}

static std::string journalBytes() {
  std::map<std::string, std::string>::const_iterator it =
      storemock::files.find(storage::JOURNAL_PATH);
  return it == storemock::files.end() ? std::string() : it->second;
}

static std::string lastLine() {
  const std::string log = journalBytes();
  size_t end = log.size();
  while (end > 0 && log[end - 1] == '\n') --end;
  const size_t previous = end ? log.rfind('\n', end - 1) : std::string::npos;
  const size_t start = previous == std::string::npos ? 0 : previous + 1;
  return log.substr(start, end - start);
}

static bool startsWith(const std::string& text, const char* prefix) {
  return text.compare(0, std::strlen(prefix), prefix) == 0;
}

static void resetStorage() {
  storage::g_mounted = false;
  storage::g_locked = false;
  storage::g_ready = false;
  storage::g_haveSeq = false;
  storage::g_lastSeq = 0;
  storage::g_lastEpoch = 0;
  storage::g_lines = 0;
  storemock::failWrite = storemock::failRead = storemock::failRename = false;
}

// A scenario always starts from a clean filesystem and clean storage globals.
static void resetFs(const char* roster) {
  storemock::files.clear();
  storemock::formats = 0;
  storemock::files["/employees.json"] = roster;
  storemock::files["/wifi.json"] = "wifi-sentinel";
  storemock::files["/index.html"] = "panel-sentinel";
  resetStorage();
}

static bool replay(const Event& event, void* core) {
  return static_cast<Attendance*>(core)->commit(event) == Result::Ok;
}

static void boot(Attendance& core) {
  resetStorage();
  CHECK(storage::mount());
  CHECK(storage::loadEmployees(core) == storage::Status::Ok);
  CHECK(storage::replayJournal(replay, &core) == storage::Status::Ok);
}

static uint32_t appendScan(Attendance& core, const char* uid, uint32_t epoch) {
  Event event;
  CHECK(core.preview(uid, epoch, event) == Result::Ok);
  const uint32_t sequence = event.sequence;
  CHECK(storage::appendEvent(event) == storage::Status::Ok);
  CHECK(core.commit(event) == Result::Ok);
  return sequence;
}

static uint32_t appendManual(Attendance& core, const char* uid, uint32_t start,
                             uint32_t end) {
  Event event;
  CHECK(core.previewManual(uid, start, end, event) == Result::Ok);
  const uint32_t sequence = event.sequence;
  CHECK(storage::appendEvent(event) == storage::Status::Ok);
  CHECK(core.commit(event) == Result::Ok);
  return sequence;
}

struct CoreState {
  uint32_t sequence = 0;
  uint32_t epoch = 0;
  uint64_t total = 0;
  bool inside = false;
  uint32_t entry = 0;
  uint32_t lastScan = 0;
  bool active = false;
  char card[21] = {};
  std::string journal;
};

static CoreState snap(const Attendance& core, const char* uid) {
  CoreState state;
  const Employee* employee = core.find(uid);
  state.sequence = core.lastSequence();
  state.epoch = core.lastEpoch();
  if (employee) {
    state.total = employee->totalSeconds;
    state.inside = employee->inside;
    state.entry = employee->entryEpoch;
    state.lastScan = employee->lastScanEpoch;
    state.active = employee->active;
    std::memcpy(state.card, employee->cardUid, sizeof(state.card));
  }
  state.journal = journalBytes();
  return state;
}

static bool unchangedAt(const char* where, const CoreState& before,
                        const CoreState& after, bool compareJournal) {
  const bool same = before.sequence == after.sequence && before.epoch == after.epoch &&
                    before.total == after.total && before.inside == after.inside &&
                    before.entry == after.entry && before.lastScan == after.lastScan &&
                    before.active == after.active &&
                    std::strcmp(before.card, after.card) == 0 &&
                    (!compareJournal || before.journal == after.journal);
  if (!same) {
    std::printf(
        "  state changed at %s: seq %lu->%lu epoch %lu->%lu total %llu->%llu "
        "inside %d->%d entry %lu->%lu lastScan %lu->%lu active %d->%d "
        "card %s->%s journal %d->%d bytes\n",
        where, (unsigned long)before.sequence, (unsigned long)after.sequence,
        (unsigned long)before.epoch, (unsigned long)after.epoch,
        (unsigned long long)before.total, (unsigned long long)after.total,
        (int)before.inside, (int)after.inside, (unsigned long)before.entry,
        (unsigned long)after.entry, (unsigned long)before.lastScan,
        (unsigned long)after.lastScan, (int)before.active, (int)after.active,
        before.card, after.card, (int)before.journal.size(),
        (int)after.journal.size());
  }
  return same;
}

// Employee/core state including the journal bytes (nothing at all changed).
static bool unchanged(const char* where, const CoreState& before,
                      const CoreState& after) {
  return unchangedAt(where, before, after, true);
}

// Employee/core state only; used after an append that is expected to grow the
// journal while the core must not have changed yet.
static bool unchangedCore(const char* where, const CoreState& before,
                          const CoreState& after) {
  return unchangedAt(where, before, after, false);
}

static uint32_t nextSequence(const Attendance& core) {
  return core.lastSequence() + 1;
}

// ---------------------------------------------------------------------------
// scenarios
// ---------------------------------------------------------------------------

// Preexisting v1 roster and v1 I/O + v2 R journal records must keep working,
// and new M records must sit next to them without rewriting anything.
static void testLegacyJournal() {
  resetFs(ROSTER_V1);
  storemock::files[storage::JOURNAL_PATH] = legacyLine("1,1,1800000000,11223344,I,0") +
                                            legacyLine("1,2,1800003600,11223344,O,3600") +
                                            legacyLine("2,3,1800003700,00000000,R,1800003600");
  Attendance core;
  boot(core);
  CHECK(core.count() == 2);
  CHECK(core.find(A) != nullptr && core.find(A)->active);           // v1 defaults
  CHECK(std::strcmp(core.find(A)->cardUid, A) == 0);
  CHECK(core.find(A)->totalSeconds == 3600);
  CHECK(!core.anyInside());
  CHECK(core.lastSequence() == 3 && core.lastEpoch() == T + 3700);
  CHECK(journalBytes() == legacyLine("1,1,1800000000,11223344,I,0") +
                              legacyLine("1,2,1800003600,11223344,O,3600") +
                              legacyLine("2,3,1800003700,00000000,R,1800003600"));

  // Old rows remain byte-identical; new named attendance uses version 4.
  appendScan(core, A, T + 3800);
  CHECK(startsWith(lastLine(), "4,") && lastLine().find(",I,0,") != std::string::npos);
  appendScan(core, A, T + 3900);
  CHECK(startsWith(lastLine(), "4,") && lastLine().find(",O,100,") != std::string::npos);

  Event manual;
  CHECK(core.previewManual(B, T + 1000, T + 1600, manual) == Result::Ok);
  CHECK(storage::appendEvent(manual) == storage::Status::Ok);
  CHECK(core.commit(manual) == Result::Ok);
  CHECK(startsWith(lastLine(), "4,") && lastLine().find(",M,600,") != std::string::npos);
  CHECK(core.lastEpoch() == T + 3900);  // M is historical: baseline stays
  CHECK(core.find(B)->totalSeconds == 600);

  Event recovery;
  CHECK(core.previewRecovery(T + 4000, recovery) == Result::Ok);
  CHECK(recovery.seconds == T + 3900);
  CHECK(storage::appendEvent(recovery) == storage::Status::Ok);
  CHECK(core.commit(recovery) == Result::Ok);
  CHECK(startsWith(lastLine(), "2,"));
  CHECK(core.lastEpoch() == T + 4000 && !core.anyInside());
  CHECK(core.find(A)->totalSeconds == 3700 && core.find(B)->totalSeconds == 600);

  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");
  CHECK(storemock::files["/index.html"] == "panel-sentinel");
  CHECK(storemock::formats == 0);
}

// previewManual is read-only and rejects every rejected shape without mutation.
static void testPreviewContract() {
  resetFs(ROSTER_V2);
  Attendance core;
  boot(core);

  CoreState before = snap(core, A);
  Event event;
  CHECK(core.previewManual(A, T - 600, T - 300, event) == Result::Ok);
  CHECK(event.type == 'M');
  CHECK(event.epoch == T - 300);
  CHECK(event.seconds == 300);
  CHECK(std::strcmp(event.uid, A) == 0);
  CHECK(event.sequence == nextSequence(core));
  CHECK(unchanged("previewManual ok", before, snap(core, A)));

  CHECK(core.previewManual(A, T - 300, T - 900, event) == Result::InvalidEvent);  // reversed
  CHECK(unchanged("previewManual reversed", before, snap(core, A)));
  CHECK(core.previewManual(A, T - 300, T - 300, event) == Result::InvalidEvent);  // zero length
  CHECK(unchanged("previewManual zero length", before, snap(core, A)));
  CHECK(core.previewManual(A, 0, T, event) == Result::InvalidEvent);              // zero start
  CHECK(unchanged("previewManual zero start", before, snap(core, A)));
  CHECK(core.previewManual(A, T - 300, 0, event) == Result::InvalidEvent);        // zero end
  CHECK(unchanged("previewManual zero end", before, snap(core, A)));
  CHECK(core.previewManual("DEADBEEF", T - 600, T - 300, event) == Result::UnknownCard);
  CHECK(unchanged("previewManual unknown", before, snap(core, A)));
  CHECK(core.previewManual("baduid", T - 600, T - 300, event) == Result::UnknownCard);
  CHECK(unchanged("previewManual malformed uid", before, snap(core, A)));
  CHECK(core.previewManual(nullptr, T - 600, T - 300, event) == Result::UnknownCard);
  CHECK(unchanged("previewManual null uid", before, snap(core, A)));

  // An open session blocks historical entries for that employee only.
  Event scan;
  CHECK(core.preview(B, T, scan) == Result::Ok);
  CHECK(storage::appendEvent(scan) == storage::Status::Ok);
  CHECK(core.commit(scan) == Result::Ok);
  before = snap(core, A);
  CHECK(core.previewManual(B, T - 600, T - 300, event) == Result::OpenSession);
  CHECK(unchanged("previewManual open session", before, snap(core, A)));
  CHECK(core.previewManual(A, T - 600, T - 300, event) == Result::Ok);
  CHECK(event.epoch == T - 300 && event.sequence == nextSequence(core));
  CHECK(unchanged("previewManual other employee", before, snap(core, A)));

  // Overflowing an accumulated total is refused at preview time.
  Employee* hostile = const_cast<Employee*>(core.find(A));
  CHECK(hostile != nullptr);
  if (hostile) {
    hostile->totalSeconds = UINT64_MAX - 10;
    CHECK(core.previewManual(A, T, T + 100, event) == Result::InvalidEvent);
    CHECK(core.previewManual(A, T, T + 10, event) == Result::Ok);
    CHECK(event.seconds == 10);
    hostile->totalSeconds = 0;
  }
  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");
  CHECK(storemock::formats == 0);
}

// Encoding, append and commit of one M record: additive seconds only.
static void testEncodeCommit() {
  resetFs(ROSTER_V1);
  Attendance core;
  boot(core);
  appendScan(core, A, T);
  appendScan(core, A, T + 600);
  CHECK(core.find(A)->totalSeconds == 600);
  CHECK(core.lastEpoch() == T + 600);

  CoreState before = snap(core, A);
  Event manual;
  CHECK(core.previewManual(A, T - 7200, T - 3600, manual) == Result::Ok);
  CHECK(manual.type == 'M' && manual.epoch == T - 3600 && manual.seconds == 3600);
  CHECK(std::strcmp(manual.uid, A) == 0);
  CHECK(manual.sequence == before.sequence + 1);
  CHECK(unchanged("previewManual before append", before, snap(core, A)));

  const size_t linesBefore = storage::snapshot().journalLines;
  const size_t bytesBefore = journalBytes().size();
  CHECK(storage::appendEvent(manual) == storage::Status::Ok);
  CHECK(storage::snapshot().journalLines == linesBefore + 1);
  CHECK(journalBytes().size() > bytesBefore);
  CHECK(unchangedCore("append alone leaves core untouched", before, snap(core, A)));

  char expected[128];
  std::snprintf(expected, sizeof(expected), "4,%lu,%lu,%s,M,3600,416C69,1,0,",
                (unsigned long)manual.sequence, (unsigned long)manual.epoch, A);
  CHECK(lastLine().size() == std::strlen(expected) + 8);
  CHECK(startsWith(lastLine(), expected));

  CHECK(core.commit(manual) == Result::Ok);
  const CoreState after = snap(core, A);
  CHECK(after.total == 600 + 3600);
  CHECK(after.sequence == manual.sequence);
  CHECK(after.epoch == T + 600);          // MAX(old, historical end): no rewind
  CHECK(!after.inside);                   // no synthetic entry
  CHECK(after.entry == 0);
  CHECK(after.lastScan == T + 600);       // cooldown/scan state untouched
  CHECK(after.journal != before.journal);

  // M never fabricates an I/O event: the next real scan still opens a session.
  Event scan;
  CHECK(core.preview(A, T + 700, scan) == Result::Ok);
  CHECK(scan.type == 'I' && scan.seconds == 0);
  CHECK(scan.sequence == after.sequence + 1);

  // A second, adjacent historical interval keeps the sequence additive.
  appendManual(core, A, T - 3600, T - 3000);
  CHECK(core.find(A)->totalSeconds == 600 + 3600 + 600);
  CHECK(core.lastEpoch() == T + 600);
  CHECK(storage::snapshot().journalLines == linesBefore + 2);
  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");
  CHECK(storemock::formats == 0);
}

// The journal identity is the stable uid; current card/active state must not
// gate historical manual entries, and the replacement card must not leak in.
static void testStableIdentityAndInactive() {
  resetFs(ROSTER_V2);
  Attendance core;
  boot(core);
  CHECK(core.updateEmployee(A, "Ali", "AABBCCDD", true) == Result::Ok);
  CHECK(std::strcmp(core.find(A)->cardUid, "AABBCCDD") == 0);

  CoreState before = snap(core, A);
  Event manual;
  CHECK(core.previewManual(A, T - 1200, T - 600, manual) == Result::Ok);
  CHECK(std::strcmp(manual.uid, A) == 0);  // stable uid, not AABBCCDD
  CHECK(unchanged("previewManual after card edit", before, snap(core, A)));
  CHECK(core.previewManual("AABBCCDD", T - 1200, T - 600, manual) == Result::UnknownCard);

  Event scan;
  CHECK(core.preview("AABBCCDD", T, scan) == Result::Ok);
  CHECK(std::strcmp(scan.uid, A) == 0);
  CHECK(storage::appendEvent(scan) == storage::Status::Ok);
  CHECK(core.commit(scan) == Result::Ok);
  CHECK(core.preview("AABBCCDD", T + 600, scan) == Result::Ok);
  CHECK(scan.type == 'O' && scan.seconds == 600);
  CHECK(storage::appendEvent(scan) == storage::Status::Ok);
  CHECK(core.commit(scan) == Result::Ok);

  appendManual(core, A, T - 1200, T - 600);
  CHECK(lastLine().find(A) != std::string::npos);
  CHECK(lastLine().find("AABBCCDD") == std::string::npos);
  CHECK(core.find(A)->totalSeconds == 600 + 600);
  CHECK(core.lastEpoch() == T + 600);

  // Inactive personnel may still receive authorised historical time.
  CHECK(core.updateEmployee(A, "Ali", "AABBCCDD", false) == Result::Ok);
  CHECK(core.preview("AABBCCDD", T + 700, scan) == Result::InactiveCard);
  before = snap(core, A);
  CHECK(core.previewManual(A, T - 2000, T - 1800, manual) == Result::Ok);
  CHECK(std::strcmp(manual.uid, A) == 0);
  CHECK(unchanged("previewManual inactive", before, snap(core, A)));
  CHECK(storage::appendEvent(manual) == storage::Status::Ok);
  CHECK(core.commit(manual) == Result::Ok);
  CHECK(core.find(A)->totalSeconds == 1400);
  CHECK(core.lastEpoch() == T + 600);  // historical M did not move the baseline
  CHECK(core.rosterVersion() != 0);

  // Persisted roster keeps card/active; replay keeps the manual total.
  CHECK(storage::saveEmployees(core, nullptr) == storage::Status::Ok);
  CHECK(storemock::files["/employees.json"].find("AABBCCDD") != std::string::npos);
  CHECK(storemock::files["/employees.json"].find("\"active\":false") != std::string::npos);
  Attendance reloaded;
  boot(reloaded);
  CHECK(reloaded.find(A) != nullptr);
  CHECK(!reloaded.find(A)->active);
  CHECK(std::strcmp(reloaded.find(A)->cardUid, "AABBCCDD") == 0);
  CHECK(reloaded.find(A)->totalSeconds == 1400);
  CHECK(reloaded.lastEpoch() == T + 600);
  CHECK(reloaded.lastSequence() == core.lastSequence());
  Event later;
  CHECK(reloaded.previewManual(A, T - 2000, T - 1900, later) == Result::Ok);
  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");
  CHECK(storemock::files["/index.html"] == "panel-sentinel");
  CHECK(storemock::formats == 0);
}

// Historical end before the clock baseline: append, replay and recovery keep
// MAX(old,end); only R may lower the baseline, and R.seconds is the baseline.
static void testHistoricalBaselineAndRecovery() {
  resetFs(ROSTER_V1);
  Attendance core;
  boot(core);
  appendScan(core, A, T);
  appendScan(core, A, T + 600);
  CHECK(core.lastEpoch() == T + 600);

  Event manual;
  CHECK(core.previewManual(A, T - 91800, T - 90000, manual) == Result::Ok);
  CHECK(manual.epoch == T - 90000 && manual.seconds == 1800);
  CHECK(storage::appendEvent(manual) == storage::Status::Ok);
  CHECK(core.commit(manual) == Result::Ok);
  CHECK(core.lastEpoch() == T + 600);
  CHECK(core.find(A)->totalSeconds == 600 + 1800);

  // Fresh boot with a historical M as the very last physical line.
  Attendance replayed;
  resetStorage();
  CHECK(storage::mount());
  CHECK(storage::loadEmployees(replayed) == storage::Status::Ok);
  CHECK(storage::replayJournal(replay, &replayed) == storage::Status::Ok);
  CHECK(replayed.lastEpoch() == T + 600);              // max, never rewound
  CHECK(replayed.lastSequence() == manual.sequence);
  CHECK(replayed.find(A)->totalSeconds == 2400);
  CHECK(!replayed.anyInside());
  CHECK(replayed.find(A)->lastScanEpoch == T + 600);

  // R still reports the prior baseline, not the historical manual end.
  Event recovery;
  CHECK(replayed.previewRecovery(T + 700, recovery) == Result::Ok);
  CHECK(recovery.type == 'R' && recovery.epoch == T + 700);
  CHECK(recovery.seconds == T + 600);

  // Clock continues forward after the historical entry.
  Event scan;
  CHECK(replayed.preview(A, T + 700, scan) == Result::Ok);
  CHECK(scan.type == 'I');
  CHECK(storage::appendEvent(scan) == storage::Status::Ok);
  CHECK(replayed.commit(scan) == Result::Ok);
  CHECK(replayed.lastEpoch() == T + 700 && replayed.anyInside());

  CHECK(storage::appendEvent(recovery) == storage::Status::BadData); // stale sequence/baseline
  CHECK(replayed.previewRecovery(T + 800, recovery) == Result::Ok);
  CHECK(recovery.seconds == T + 700);
  CHECK(storage::appendEvent(recovery) == storage::Status::Ok);
  CHECK(replayed.commit(recovery) == Result::Ok);
  CHECK(replayed.lastEpoch() == T + 800 && !replayed.anyInside());
  CHECK(replayed.find(A)->totalSeconds == 2400);  // R cancels, adds no time
  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");
  CHECK(storemock::formats == 0);
}

// Overlap validation covers completed O and M intervals of the same stable uid.
static void testOverlap() {
  resetFs(ROSTER_V2);
  Attendance core;
  boot(core);
  appendScan(core, A, T);                    // I
  appendScan(core, A, T + 3600);             // O -> [T, T+3600)
  appendManual(core, A, T - 7200, T - 3600); // M -> [T-7200, T-3600)
  appendScan(core, B, T + 50000);            // I
  appendScan(core, B, T + 50300);            // O -> [T+50000, T+50300)

  const std::string journal = journalBytes();
  bool hit = false;
  CHECK(storage::manualOverlap(A, T - 7200, T - 3600, hit) == storage::Status::Ok);
  CHECK(hit);  // duplicate of the manual interval
  hit = true;
  CHECK(storage::manualOverlap(A, T - 7100, T - 7000, hit) == storage::Status::Ok && hit);
  hit = false;
  CHECK(storage::manualOverlap(A, T + 100, T + 200, hit) == storage::Status::Ok && hit);
  hit = false;
  CHECK(storage::manualOverlap(A, T - 3600, T + 100, hit) == storage::Status::Ok && hit);
  hit = true;
  CHECK(storage::manualOverlap(A, T - 7300, T - 7200, hit) == storage::Status::Ok && !hit);
  hit = true;
  CHECK(storage::manualOverlap(A, T - 3600, T, hit) == storage::Status::Ok && !hit);
  hit = true;
  CHECK(storage::manualOverlap(A, T + 3600, T + 7200, hit) == storage::Status::Ok && !hit);
  hit = false;
  CHECK(storage::manualOverlap(B, T + 50000, T + 50100, hit) == storage::Status::Ok && hit);
  hit = true;
  CHECK(storage::manualOverlap(B, T - 7200, T - 3600, hit) == storage::Status::Ok && !hit);
  hit = true;
  CHECK(storage::manualOverlap(A, T + 50000, T + 50100, hit) == storage::Status::Ok && !hit);

  // Argument validation; overlap is always cleared on failure.
  hit = true;
  CHECK(storage::manualOverlap(A, 0, 100, hit) == storage::Status::BadData && !hit);
  hit = true;
  CHECK(storage::manualOverlap("BADUID", T, T + 10, hit) == storage::Status::BadData && !hit);
  hit = true;
  CHECK(storage::manualOverlap(nullptr, T, T + 10, hit) == storage::Status::BadData && !hit);
  hit = true;
  CHECK(storage::manualOverlap("aabbcc44", T, T + 10, hit) == storage::Status::BadData && !hit);
  hit = true;
  CHECK(storage::manualOverlap(A, T, T, hit) == storage::Status::BadData && !hit);
  hit = true;
  CHECK(storage::manualOverlap(A, T + 10, T, hit) == storage::Status::BadData && !hit);
  hit = true;
  CHECK(storage::manualOverlap(A, T, cfg::MAX_EPOCH + 1u, hit) == storage::Status::BadData && !hit);
  hit = true;
  CHECK(storage::manualOverlap(A, cfg::MIN_EPOCH - 1u, cfg::MIN_EPOCH + 10u, hit) ==
        storage::Status::BadData && !hit);
  CHECK(journalBytes() == journal);  // validation never writes

  // Validation leaves the store usable for the append that follows it.
  appendManual(core, A, T - 200000, T - 100000);
  CHECK(core.find(A)->totalSeconds == 3600 + 3600 + 100000);

  // An open session cancelled by R is NOT worked time for overlap purposes.
  appendScan(core, A, T + 100000);
  Event recovery;
  CHECK(core.previewRecovery(T + 100010, recovery) == Result::Ok);
  CHECK(recovery.seconds == T + 100000);
  CHECK(storage::appendEvent(recovery) == storage::Status::Ok);
  CHECK(core.commit(recovery) == Result::Ok);
  hit = true;
  CHECK(storage::manualOverlap(A, T + 100000, T + 100005, hit) == storage::Status::Ok && !hit);
  hit = false;
  CHECK(storage::manualOverlap(A, T, T + 3600, hit) == storage::Status::Ok && hit);
  hit = false;
  CHECK(storage::manualOverlap(A, T - 150000, T - 149900, hit) == storage::Status::Ok && hit);

  // Integrity checking continues after an overlap was already found.
  const std::string good = journalBytes();
  const uint32_t next = nextSequence(core);
  std::string tampered = manualLine(next, T + 1000, A, 600);
  tampered[10] = tampered[10] == '1' ? '2' : '1';  // body changed, CRC now stale
  storemock::files[storage::JOURNAL_PATH] = good + tampered;
  const std::string corrupt = journalBytes();
  hit = true;
  CHECK(storage::manualOverlap(A, T + 100, T + 200, hit) == storage::Status::IntegrityError && !hit);
  CHECK(storage::locked()); // matching old O did not skip the corrupt later line
  hit = true;
  CHECK(storage::manualOverlap(A, T + 100, T + 200, hit) == storage::Status::Locked && !hit);
  CHECK(journalBytes() == corrupt);  // no truncation, no repair
  CHECK(storemock::formats == 0);
  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");
  CHECK(storemock::files["/index.html"] == "panel-sentinel");
}

// Incomplete or corrupt appends and M lines must fail closed.
static void testFailClosed() {
  // (1) An append that cannot be written durably never reaches commit.
  resetFs(ROSTER_V2);
  Attendance core;
  boot(core);
  appendScan(core, A, T);
  appendScan(core, A, T + 600);
  CoreState before = snap(core, A);
  Event manual;
  CHECK(core.previewManual(A, T - 7200, T - 3600, manual) == Result::Ok);
  storemock::files["/pad.bin"] = std::string((size_t)1048576, 'x');  // no free space
  CHECK(storage::appendEvent(manual) == storage::Status::NoSpace);
  CHECK(storage::locked());
  CHECK(unchanged("M append without space", before, snap(core, A)));
  storemock::files.erase("/pad.bin");
  CHECK(storage::appendEvent(manual) == storage::Status::Locked);  // stays locked
  Attendance noSpace;
  resetStorage();
  CHECK(storage::mount());
  CHECK(storage::loadEmployees(noSpace) == storage::Status::Ok);
  CHECK(storage::replayJournal(replay, &noSpace) == storage::Status::Ok);
  CHECK(noSpace.find(A)->totalSeconds == 600);  // M was never recorded
  CHECK(noSpace.lastEpoch() == T + 600);

  // (1b) A torn append is a partial, unverifiable tail: reboot fails closed.
  resetFs(ROSTER_V2);
  Attendance torn;
  boot(torn);
  appendScan(torn, A, T);
  appendScan(torn, A, T + 600);
  Event tornEvent;
  CHECK(torn.previewManual(A, T - 7200, T - 3600, tornEvent) == Result::Ok);
  const CoreState tornState = snap(torn, A);
  storemock::failWrite = true;
  CHECK(storage::appendEvent(tornEvent) == storage::Status::IoError);
  CHECK(storage::locked());
  CHECK(torn.lastSequence() == tornState.sequence);
  CHECK(torn.lastEpoch() == tornState.epoch);
  CHECK(torn.find(A)->totalSeconds == tornState.total);  // never committed
  storemock::failWrite = false;
  const std::string tornBytes = journalBytes();
  CHECK(tornBytes.size() > tornState.journal.size());  // partial bytes on disk
  resetStorage();
  Attendance recovered;
  CHECK(storage::mount());
  CHECK(storage::loadEmployees(recovered) == storage::Status::Ok);
  CHECK(storage::replayJournal(replay, &recovered) == storage::Status::IntegrityError);
  CHECK(storage::locked());
  CHECK(journalBytes() == tornBytes);  // no truncation, no silent repair
  CHECK(storage::appendEvent(tornEvent) == storage::Status::Locked);
  CHECK(storemock::formats == 0);

  // (2) Malformed M events are rejected by both append and commit.
  resetFs(ROSTER_V1);
  Attendance healthy;
  boot(healthy);
  appendScan(healthy, A, T);
  appendScan(healthy, A, T + 600);
  CHECK(healthy.previewManual(A, T - 7200, T - 3600, manual) == Result::Ok);
  before = snap(healthy, A);
  const std::string clean = journalBytes();

  Event bad = manual;
  bad.sequence = manual.sequence + 1;                     // sequence gap
  CHECK(storage::appendEvent(bad) == storage::Status::BadData);
  CHECK(healthy.commit(bad) == Result::InvalidEvent);
  CHECK(unchanged("M sequence gap", before, snap(healthy, A)));

  bad = manual;
  bad.sequence = manual.sequence - 1;                     // replay of old sequence
  CHECK(storage::appendEvent(bad) == storage::Status::BadData);
  CHECK(healthy.commit(bad) == Result::InvalidEvent);
  CHECK(unchanged("M stale sequence", before, snap(healthy, A)));

  bad = manual;
  bad.seconds = 0;                                        // incomplete interval
  CHECK(storage::appendEvent(bad) == storage::Status::BadData);
  CHECK(healthy.commit(bad) == Result::InvalidEvent);
  CHECK(unchanged("M zero duration", before, snap(healthy, A)));

  bad = manual;
  bad.seconds = bad.epoch;                                // start would be epoch 0
  CHECK(storage::appendEvent(bad) == storage::Status::BadData);
  CHECK(healthy.commit(bad) == Result::InvalidEvent);
  CHECK(unchanged("M whole-epoch duration", before, snap(healthy, A)));

  bad = manual;
  bad.epoch = cfg::MAX_EPOCH + 1u;                        // untrustworthy clock
  CHECK(storage::appendEvent(bad) == storage::Status::BadData);
  CHECK(unchanged("M epoch beyond MAX", before, snap(healthy, A)));

  bad = manual;
  std::strcpy(bad.uid, "55aa7788");                       // non-canonical journal uid
  CHECK(storage::appendEvent(bad) == storage::Status::BadData);
  CHECK(healthy.commit(bad) == Result::InvalidEvent);
  CHECK(unchanged("M lowercase uid", before, snap(healthy, A)));

  bad = manual;
  std::strcpy(bad.uid, "DEADBEEF");                       // unknown stable identity
  CHECK(healthy.commit(bad) == Result::UnknownCard);
  CHECK(unchanged("M unknown employee", before, snap(healthy, A)));

  bad = manual;
  bad.type = 'X';                                         // no invented event types
  bad.epoch = T + 700; bad.seconds = 0;                    // isolate type, not backwards clock
  CHECK(healthy.commit(bad) == Result::InvalidEvent);
  CHECK(unchanged("M wrong type", before, snap(healthy, A)));

  // Open session blocks commit even when the event was crafted by hand.
  Event scan;
  CHECK(healthy.preview(A, T + 700, scan) == Result::Ok);
  CHECK(storage::appendEvent(scan) == storage::Status::Ok);
  CHECK(healthy.commit(scan) == Result::Ok);
  CHECK(healthy.anyInside());
  before = snap(healthy, A);
  Event insideManual;
  CHECK(healthy.previewManual(A, T - 7200, T - 3600, insideManual) == Result::OpenSession);
  insideManual.sequence = nextSequence(healthy);
  insideManual.epoch = T + 800;
  std::strcpy(insideManual.uid, A);
  insideManual.type = 'M';
  insideManual.seconds = 600;
  CHECK(storage::appendEvent(insideManual) == storage::Status::Ok);  // journal level is valid
  CHECK(healthy.commit(insideManual) == Result::InvalidEvent);
  CHECK(unchangedCore("M while inside", before, snap(healthy, A)));
  CHECK(healthy.anyInside());

  // Existing journal bytes are untouched by all rejected operations.
  CHECK(journalBytes().compare(0, clean.size(), clean) == 0);
  CHECK(!storage::locked());  // BadData/invalid input never locks by itself
  CHECK(storemock::formats == 0);
  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");

  // (3) Replay rejects structurally or semantically broken M lines.
  const char* const corruptJournals[] = {
    // valid CRC, duration >= end epoch (start would be 0)
    "3,2,1800000000,11223344,M,1800000000",
    // valid CRC, start below MIN_EPOCH
    "3,2,1704077300,11223344,M,1704067200",
    // valid CRC, sequence gap after the first line
    "3,3,1800000100,11223344,M,100",
    // valid CRC, zero duration is not a completed interval
    "3,2,1800000100,11223344,M,0",
    // valid CRC, non-numeric duration
    "3,2,1800000100,11223344,M,100x",
  };
  for (const char* text : corruptJournals) {
    resetFs(ROSTER_V1);
    storemock::files[storage::JOURNAL_PATH] =
        legacyLine("1,1,1800000000,11223344,I,0") + legacyLine(text);
    const std::string bytes = journalBytes();
    resetStorage();
    Attendance corrupt;
    CHECK(storage::mount());
    CHECK(storage::loadEmployees(corrupt) == storage::Status::Ok);
    CHECK(storage::replayJournal(replay, &corrupt) == storage::Status::IntegrityError);
    CHECK(storage::locked());
    CHECK(journalBytes() == bytes);
    bool hit = true;
    CHECK(storage::manualOverlap(A, T + 100, T + 200, hit) == storage::Status::Locked && !hit);
  }

  // Valid but truncated trailing M line (power loss mid-append).
  resetFs(ROSTER_V1);
  {
    const std::string complete = legacyLine("1,1,1800000000,11223344,I,0") +
                                 legacyLine("2,2,1800000100,00000000,R,1800000000") +
                                 manualLine(3, 1800000200, A, 100);
    CHECK(complete[complete.size() - 1] == '\n');
    storemock::files[storage::JOURNAL_PATH] = complete.substr(0, complete.size() - 1);
  }
  const std::string truncated = journalBytes();
  resetStorage();
  Attendance partial;
  CHECK(storage::mount());
  CHECK(storage::loadEmployees(partial) == storage::Status::Ok);
  CHECK(storage::replayJournal(replay, &partial) == storage::Status::IntegrityError);
  CHECK(storage::locked());
  CHECK(journalBytes() == truncated);
  CHECK(storemock::formats == 0);

  // (4) A well-formed handcrafted M replays additively.
  resetFs(ROSTER_V1);
  storemock::files[storage::JOURNAL_PATH] =
      legacyLine("1,1,1800000000,11223344,I,0") +
      legacyLine("1,2,1800003600,11223344,O,3600") +
      manualLine(3, 1800000100, A, 100) + manualLine(4, 1800000300, A, 100);
  Attendance replayCore;
  boot(replayCore);
  CHECK(replayCore.find(A)->totalSeconds == 3800);
  CHECK(replayCore.lastSequence() == 4);
  CHECK(replayCore.lastEpoch() == T + 3600);
  CHECK(!replayCore.anyInside());

  // (5) Accumulated-total overflow is refused at commit too.
  Employee* hostile = const_cast<Employee*>(replayCore.find(A));
  CHECK(hostile != nullptr);
  if (hostile) {
    hostile->totalSeconds = UINT64_MAX - 10;
    const CoreState maxed = snap(replayCore, A);
    Event over;
    over.sequence = nextSequence(replayCore);
    over.epoch = T + 4000;
    std::strcpy(over.uid, A);
    over.type = 'M';
    over.seconds = 11;
    CHECK(replayCore.commit(over) == Result::InvalidEvent);
    CHECK(unchanged("M total overflow", maxed, snap(replayCore, A)));
    Event exact = over;
    exact.seconds = 10;
    CHECK(replayCore.commit(exact) == Result::Ok);
    CHECK(replayCore.find(A)->totalSeconds == UINT64_MAX);
    Event none;
    CHECK(replayCore.previewManual(A, T + 4000, T + 4001, none) == Result::InvalidEvent);
  }
  CHECK(storemock::files["/wifi.json"] == "wifi-sentinel");
  CHECK(storemock::files["/index.html"] == "panel-sentinel");
  CHECK(storemock::formats == 0);
}

static void testForwardManualBoundary() {
  resetFs(ROSTER_V1);
  Attendance core;
  boot(core);
  appendManual(core,A,T,T+3600);
  CHECK(core.lastEpoch()==T+3600 && storage::g_lastEpoch==T+3600);
  CHECK(core.find(A)->totalSeconds==3600 && !core.anyInside());
  CHECK(core.find(A)->lastScanEpoch==0);
  Attendance reloaded;
  boot(reloaded);
  CHECK(reloaded.lastEpoch()==T+3600 && storage::g_lastEpoch==T+3600);
  CHECK(reloaded.find(A)->totalSeconds==3600);
  Event scan,recovery;
  CHECK(reloaded.preview(B,T+3000,scan)==Result::ClockBackwards);
  CHECK(reloaded.previewRecovery(T+7200,recovery)==Result::Ok);
  CHECK(recovery.seconds==T+3600);
  CHECK(storage::appendEvent(recovery)==storage::Status::Ok);
  CHECK(reloaded.commit(recovery)==Result::Ok);
}

int main() {
  testLegacyJournal();
  testPreviewContract();
  testEncodeCommit();
  testStableIdentityAndInactive();
  testHistoricalBaselineAndRecovery();
  testOverlap();
  testFailClosed();
  testForwardManualBoundary();
  std::printf("manual attendance: %d checks, %d failure(s)\n", checks, failures);
  return failures ? 1 : 0;
}
