#pragma once
#include <stddef.h>
#include <stdint.h>

namespace pdks {
constexpr size_t MAX_EMPLOYEES = 32;
constexpr uint32_t RESCAN_SECONDS = 5;

struct Employee {
  char uid[21] = {};
  char name[49] = {};
  char cardUid[21] = {}; // mutable card; uid remains the journal identity
  bool active = true;
  bool inside = false;
  uint32_t entryEpoch = 0;
  uint32_t lastScanEpoch = 0;
  uint64_t totalSeconds = 0;
};

struct Event {
  uint32_t sequence = 0;
  uint32_t epoch = 0;
  char uid[21] = {};
  char type = 'I';
  uint32_t seconds = 0;
  // Employee name snapshot at record time; empty for legacy journal rows.
  char name[49] = {};
  bool nameRecorded = false; // true when name is a validated snapshot
  bool archived = false;     // true for history of removed employees
};

enum class Result {
  Ok, InvalidUid, InvalidName, Full, UnknownCard, TooSoon,
   ClockBackwards, InvalidEvent, CardInUse, InactiveCard, OpenSession
};

// No I/O: preview never mutates state; commit only AFTER durable journal append.
class Attendance {
 public:
  void clear();
  Result upsertEmployee(const char* uid, const char* name);
  Result updateEmployee(const char* uid, const char* name, const char* cardUid, bool active);
  // Adds a new stable identity with an explicit card (card reissue after removal).
  // Identity and card must both be unused; no state changes on failure.
  // Identity/card conflicts report Result::CardInUse.
  Result addEmployee(const char* identity, const char* name, const char* cardUid,
                     bool active = true);
  // Removes a roster entry only when it has no open session. History and the
  // journal clock/sequence are untouched. Unknown identity -> UnknownCard.
  Result removeEmployee(const char* uid);
  Result previewRecovery(uint32_t epoch, Event& event) const;
  Result previewManual(const char* uid, uint32_t start, uint32_t end, Event& event) const;
  Result preview(const char* uid, uint32_t epoch, Event& event) const;
  // Used both after append and during journal replay; validates sequence/state.
  Result commit(const Event& event);
  const Employee* find(const char* uid) const;
  const Employee* findByCard(const char* uid) const;
  uint32_t rosterVersion() const;
  const Employee* at(size_t index) const;
  size_t count() const { return count_; }
  uint32_t lastSequence() const { return lastSequence_; }
  uint32_t lastEpoch() const { return lastEpoch_; }
  bool anyInside() const;
  static bool normalizeUid(const char* input, char output[21]);
 private:
  Result previewStored(const Employee& employee, uint32_t epoch, Event& event) const;
  Result commitArchived(const Event& event);
  Employee employees_[MAX_EMPLOYEES] = {};
  size_t count_ = 0;
  uint32_t lastSequence_ = 0;
  uint32_t lastEpoch_ = 0;
};
const char* resultText(Result result);
}  // namespace pdks
