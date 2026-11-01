#pragma once

// ---------------------------------------------------------------------------
// Internal persistence for the attendance terminal.
//
// Layout (LittleFS, always mounted with auto-format DISABLED):
//   /employees.json  roster v2 (reads legacy v1), atomic tmp+rename
//   /journal.log     append-only event log, one CSV-ish line per event:
//                    1,<seq>,<epoch>,<uid>,<I|O>,<seconds>,<CRC32 hex>
//                    2,<seq>,<new epoch>,00000000,R,<old epoch>,<CRC32 hex>
//                    R cancels open sessions WITHOUT worked time, starts a new
//                    clock baseline, and preserves all preceding log bytes.
//                    3,<seq>,<end epoch>,<stable uid>,M,<duration>,<CRC32 hex>
//                    M is an explicitly authorised historical manual interval;
//                    start=end-duration; it never rewinds the clock baseline.
//   v2 roster adds card_uid/active; uid stays the immutable journal identity.
//   Older firmware must NOT be used after incompatible v2 roster/R or v3/M writes.
//                    CRC32 (IEEE) covers everything before the last comma.
//   /index.html      optional operator UI (uploaded separately)
//
// The journal is authoritative: totals and inside/outside state are rebuilt by
// replaying it at boot. Corrupted, truncated or unverifiable logs fail closed:
// storage is locked and no automatic repair, truncation or rotation happens.
//
// All functions are blocking but short; replay/export loops must be driven by
// the caller through callbacks so it can yield().
// ---------------------------------------------------------------------------

#include <stddef.h>
#include <stdint.h>

#include "Attendance.h"
#include "Config.h"

namespace pdks {
namespace storage {

enum class Status : uint8_t {
  Ok = 0,
  NotMounted,
  Locked,
  IoError,
  IntegrityError,
  NoSpace,
  BadData,
};

const char* statusText(Status status);

constexpr size_t JOURNAL_MAX_LINE = cfg::JOURNAL_MAX_LINE;
constexpr uint32_t MIN_FREE_BYTES = cfg::FS_MIN_FREE_BYTES;
constexpr size_t EMPLOYEES_JSON_MAX = cfg::EMPLOYEES_JSON_MAX;

constexpr const char* EMPLOYEES_PATH = "/employees.json";
constexpr const char* EMPLOYEES_TMP_PATH = "/employees.tmp";
constexpr const char* JOURNAL_PATH = "/journal.log";
constexpr const char* INDEX_PATH = "/index.html";
constexpr const char* SD_EVENTS_PATH = "/events.log";

struct Snapshot {
  bool mounted = false;
  bool locked = false;
  bool employeesPresent = false;
  bool journalPresent = false;
  uint32_t employeesBytes = 0;
  uint32_t journalBytes = 0;
  uint32_t journalLines = 0;
  uint32_t freeBytes = 0;
};

// Mounts LittleFS with setAutoFormat(false). Never formats. Idempotent.
bool mount();
bool mounted();
bool locked();
Snapshot snapshot();

// Explicit operator format ("FORMAT YES" on serial). Honoured only when the
// filesystem did not mount or when the device is blank (no roster, no journal).
// Existing records are never deleted by this call.
Status operatorFormat();

// Durable append: write -> flush -> close -> re-open and byte-compare the tail.
// Returns Ok only after the read-back matches; any failure locks storage.
Status appendEvent(const Event& event);

// Loads the roster only (identity); attendance state comes from the journal.
// A missing file is a valid empty roster.
Status loadEmployees(Attendance& core);

// Writes the roster to a temp file, parses it back for verification, then
// renames it over employees.json. `candidate` (may be null) supplies a new
// employee or a name override for an existing uid.
Status saveEmployees(const Attendance& core, const Employee* candidate);

// Visitor returns false to abort (treated as an integrity failure).
using EventVisitor = bool (*)(const Event& event, void* ctx);

// Streams the journal, validating structure, CRC, sequence/epoch continuity.
// Any corrupt or partial line locks storage (fail closed, no silent skipping).
// Passing a null visitor performs validation only.
Status replayJournal(EventVisitor visitor, void* ctx);

// Validate the journal and check completed O/M intervals for this stable UID.
// Adjacent [start,end) ranges are allowed; overlaps and duplicates are reported.
Status manualOverlap(const char* uid, uint32_t start, uint32_t end, bool& overlap);

// Explicit destructive operations only. Completed I/O sessions are removed as
// a pair, M singly; R/open sessions are not deletable. Rewrites are verified
// before one atomic generation selector commit, then old files/SD are cleaned.
uint32_t journalRevision();
Status deleteEmployee(Attendance& core, const char* uid);
Status deleteSession(Attendance& core, uint32_t targetSequence);
Status uidHasHistory(const char* uid, bool& found);
Status completePendingRewrite();

// Optional microSD mirror (canonical data stays on LittleFS).
bool sdEnabled();
bool sdOk();
bool beginSd();

} // namespace storage
} // namespace pdks
