#include "Attendance.h"

#include <stdint.h>
#include <string.h>

namespace pdks {
namespace {

// Struct capacities, including the terminating NUL byte.
constexpr size_t UID_CAPACITY = sizeof(((Employee*)0)->uid);
constexpr size_t NAME_CAPACITY = sizeof(((Employee*)0)->name);

static_assert(sizeof(((Event*)0)->name) == NAME_CAPACITY,
              "Event::name must match Employee::name capacity");

bool isHexDigit(char c) {
  return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
         (c >= 'A' && c <= 'F');
}

char upperHex(char c) {
  if (c >= 'a' && c <= 'f') {
    return static_cast<char>(c - 'a' + 'A');
  }
  return c;
}

bool isWhitespaceByte(unsigned char c) {
  return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' ||
         c == '\f';
}

// 1..48 bytes, no ASCII control characters, at least one non-whitespace byte.
// Bytes >= 0x80 are treated as opaque UTF-8 continuation/lead bytes.
bool isValidName(const char* name) {
  if (name == nullptr) {
    return false;
  }
  size_t length = 0;
  while (length < NAME_CAPACITY && name[length] != '\0') {
    ++length;
  }
  if (length == 0 || length >= NAME_CAPACITY) {
    return false;
  }
  bool hasVisible = false;
  for (size_t i = 0; i < length; ++i) {
    const unsigned char c = static_cast<unsigned char>(name[i]);
    if (c < 0x20 || c == 0x7F) {
      return false;
    }
    if (!isWhitespaceByte(c)) {
      hasVisible = true;
    }
  }
  return hasVisible;
}

void copyName(char destination[NAME_CAPACITY], const char* source) {
  // Measure before touching destination: source may point into destination
  // (self-rename) or overlap it.
  size_t length = 0;
  while (length < NAME_CAPACITY && source[length] != '\0') {
    ++length;
  }
  if (length >= NAME_CAPACITY) {  // Caller validates names; stay defensive.
    destination[0] = '\0';
    return;
  }
  memmove(destination, source, length + 1);
  memset(destination + length + 1, 0, NAME_CAPACITY - length - 1);
}

// Snapshot the roster name so historical rows survive later renames.
void captureName(Event& event, const char* source) {
  copyName(event.name, source);
  event.nameRecorded = true;
}

bool equalsCanonical(const char* raw, const char* canonical) {
  size_t i = 0;
  while (canonical[i] != '\0') {
    if (raw[i] != canonical[i]) {
      return false;
    }
    ++i;
  }
  return raw[i] == '\0';
}

}  // namespace

bool Attendance::normalizeUid(const char* input, char output[21]) {
  if (input == nullptr || output == nullptr) {
    return false;
  }
  // Scan at most one byte past the longest accepted UID.
  size_t length = 0;
  while (length <= 20 && input[length] != '\0') {
    ++length;
  }
  if (length != 8 && length != 14 && length != 20) {
    return false;
  }
  for (size_t i = 0; i < length; ++i) {
    if (!isHexDigit(input[i])) {
      return false;
    }
    output[i] = upperHex(input[i]);
  }
  output[length] = '\0';
  return true;
}

void Attendance::clear() {
  for (size_t i=0; i<count_; ++i) employees_[i] = Employee();
  count_ = 0;
  lastSequence_ = 0;
  lastEpoch_ = 0;
}

Result Attendance::upsertEmployee(const char* uid, const char* name) {
  char normalized[UID_CAPACITY];
  if (!normalizeUid(uid, normalized)) {
    return Result::InvalidUid;
  }
  if (!isValidName(name)) {
    return Result::InvalidName;
  }
  for (size_t i = 0; i < count_; ++i) {
    if (strcmp(employees_[i].uid, normalized) == 0) {
      // Rename only: keep inside/entryEpoch/lastScanEpoch/totalSeconds.
      copyName(employees_[i].name, name);
      return Result::Ok;
    }
  }
  if (count_ >= MAX_EMPLOYEES) {
    return Result::Full;
  }
  if (findByCard(normalized)) return Result::CardInUse;
  Employee& employee = employees_[count_];
  employee = Employee();
  memcpy(employee.uid, normalized, strlen(normalized) + 1);
  memcpy(employee.cardUid, normalized, strlen(normalized) + 1);
  copyName(employee.name, name);
  ++count_;
  return Result::Ok;
}

Result Attendance::updateEmployee(const char* uid, const char* name,
                                  const char* cardUid, bool active) {
  char canonical[21];
  if (!normalizeUid(cardUid, canonical)) return Result::InvalidUid;
  if (!isValidName(name)) return Result::InvalidName;
  const Employee* found = find(uid);
  if (!found) return Result::UnknownCard;
  for (size_t i = 0; i < count_; ++i) {
    const Employee& other = employees_[i];
    if (&other != found && (strcmp(other.cardUid, canonical) == 0 ||
                            strcmp(other.uid, canonical) == 0)) return Result::CardInUse;
  }
  if (found->inside && (!active || strcmp(found->cardUid, canonical) != 0))
    return Result::OpenSession;
  Employee* employee = &employees_[static_cast<size_t>(found - employees_)];
  copyName(employee->name, name);
  memmove(employee->cardUid, canonical, strlen(canonical) + 1);
  employee->active = active;
  return Result::Ok;
}

Result Attendance::addEmployee(const char* identity, const char* name,
                               const char* cardUid, bool active) {
  char canonicalUid[UID_CAPACITY];
  char canonicalCard[UID_CAPACITY];
  if (!normalizeUid(identity, canonicalUid)) return Result::InvalidUid;
  if (!normalizeUid(cardUid, canonicalCard)) return Result::InvalidUid;
  if (!isValidName(name)) return Result::InvalidName;
  if (count_ >= MAX_EMPLOYEES) return Result::Full;
  for (size_t i = 0; i < count_; ++i) {
    const Employee& other = employees_[i];
    // A new identity is explicit: it must not recycle an existing one, and
    // neither identity nor card may collide across the two namespaces.
    if (strcmp(other.uid, canonicalUid) == 0) return Result::CardInUse;
    if (strcmp(other.cardUid, canonicalCard) == 0) return Result::CardInUse;
    if (strcmp(other.cardUid, canonicalUid) == 0) return Result::CardInUse;
    if (strcmp(other.uid, canonicalCard) == 0) return Result::CardInUse;
  }
  Employee& employee = employees_[count_];
  employee = Employee();
  memcpy(employee.uid, canonicalUid, strlen(canonicalUid) + 1);
  memcpy(employee.cardUid, canonicalCard, strlen(canonicalCard) + 1);
  copyName(employee.name, name);
  employee.active = active;
  ++count_;
  return Result::Ok;
}

Result Attendance::removeEmployee(const char* uid) {
  char normalized[UID_CAPACITY];
  if (!normalizeUid(uid, normalized)) return Result::InvalidUid;
  for (size_t i = 0; i < count_; ++i) {
    if (strcmp(employees_[i].uid, normalized) != 0) continue;
    // Only a closed session can be deleted; history stays in the journal.
    if (employees_[i].inside) return Result::OpenSession;
    for (size_t j = i; j + 1 < count_; ++j) {
      employees_[j] = employees_[j + 1];
    }
    employees_[count_ - 1] = Employee();
    --count_;
    return Result::Ok; // no total, sequence or clock change
  }
  return Result::UnknownCard;
}

Result Attendance::preview(const char* uid, uint32_t epoch, Event& event) const {
  char normalized[UID_CAPACITY];
  if (!normalizeUid(uid, normalized)) {
    return Result::InvalidUid;
  }
  if (epoch == 0) {
    return Result::InvalidEvent;
  }
  const Employee* employee = findByCard(normalized);
  if (employee == nullptr) {
    return Result::UnknownCard;
  }
  if (!employee->active) return Result::InactiveCard;
  return previewStored(*employee, epoch, event);
}

Result Attendance::previewStored(const Employee& stored, uint32_t epoch, Event& event) const {
  const Employee* employee = &stored;
  if (!epoch) return Result::InvalidEvent;
  if (epoch < lastEpoch_) {
    return Result::ClockBackwards;
  }
  if (employee->lastScanEpoch != 0 && epoch < employee->lastScanEpoch) {
    return Result::ClockBackwards;
  }
  if (employee->lastScanEpoch != 0 &&
      (epoch - employee->lastScanEpoch) < RESCAN_SECONDS) {
    return Result::TooSoon;
  }
  if (lastSequence_ == UINT32_MAX) {
    return Result::InvalidEvent;
  }

  Event next;
  next.sequence = lastSequence_ + 1;
  next.epoch = epoch;
  memcpy(next.uid, employee->uid, strlen(employee->uid) + 1);
  if (!employee->inside) {
    next.type = 'I';
    next.seconds = 0;
  } else {
    next.type = 'O';
    next.seconds = epoch - employee->entryEpoch;
  }
  captureName(next, employee->name);
  event = next;
  return Result::Ok;
}

Result Attendance::commit(const Event& event) {
  // The name payload is optional for legacy rows but must be usable when set.
  if (event.name[0] != '\0' || event.nameRecorded) {
    if (!isValidName(event.name)) return Result::InvalidName;
  }
  if (event.archived) return commitArchived(event);
  if (event.type == 'R') {
    if (lastSequence_ == UINT32_MAX || event.sequence != lastSequence_ + 1 ||
        !event.epoch || strcmp(event.uid, "00000000") != 0 || event.seconds != lastEpoch_)
      return Result::InvalidEvent;
    for (size_t i = 0; i < count_; ++i) {
      employees_[i].inside = false;
      employees_[i].entryEpoch = 0;
      employees_[i].lastScanEpoch = 0;
    }
    lastSequence_ = event.sequence;
    lastEpoch_ = event.epoch;
    return Result::Ok;
  }
  char normalized[UID_CAPACITY];
  if (!normalizeUid(event.uid, normalized)) {
    return Result::InvalidUid;
  }
  // Journal entries must already be canonical uppercase.
  if (!equalsCanonical(event.uid, normalized)) {
    return Result::InvalidEvent;
  }
  if (lastSequence_ == UINT32_MAX) {
    return Result::InvalidEvent;
  }
  if (event.sequence != lastSequence_ + 1) {
    return Result::InvalidEvent;
  }
  if (event.epoch == 0) {
    return Result::InvalidEvent;
  }

  Employee* employee = nullptr;
  for (size_t i = 0; i < count_; ++i) {
    if (strcmp(employees_[i].uid, normalized) == 0) {
      employee = &employees_[i];
      break;
    }
  }
  if (employee == nullptr) {
    return Result::UnknownCard;
  }

  if (event.type == 'M') {
    if (!event.seconds || event.seconds >= event.epoch || employee->inside ||
        UINT64_MAX - employee->totalSeconds < event.seconds) return Result::InvalidEvent;
    employee->totalSeconds += event.seconds;
    lastSequence_ = event.sequence;
    if (event.epoch > lastEpoch_) lastEpoch_ = event.epoch;
    return Result::Ok; // no synthetic scan, entry state or cooldown change
  }

  // Reuse preview so replay enforces exactly the same rules as live scanning.
  Event expected;
  // Replay uses stable identity, even if its current card/name/active state changed.
  const Result check = previewStored(*employee, event.epoch, expected);
  if (check != Result::Ok) {
    return check;
  }
  if (event.type != expected.type || event.seconds != expected.seconds) {
    return Result::InvalidEvent;
  }

  if (event.type == 'I') {
    employee->inside = true;
    employee->entryEpoch = event.epoch;
    employee->lastScanEpoch = event.epoch;
  } else if (event.type == 'O') {
    employee->inside = false;
    employee->entryEpoch = 0;
    employee->lastScanEpoch = event.epoch;
    employee->totalSeconds += static_cast<uint64_t>(event.seconds);
  } else {
    return Result::InvalidEvent;
  }

  lastSequence_ = event.sequence;
  lastEpoch_ = event.epoch;
  return Result::Ok;
}

// Historical rows of removed employees: they consume the sequence and clock
// like a live record but never restore a roster entry, totals or open state.
Result Attendance::commitArchived(const Event& event) {
  if (event.type != 'I' && event.type != 'O' && event.type != 'M') {
    return Result::InvalidEvent; // recovery and unknown types never archive
  }
  char normalized[UID_CAPACITY];
  if (!normalizeUid(event.uid, normalized)) return Result::InvalidUid;
  if (!equalsCanonical(event.uid, normalized)) return Result::InvalidEvent;
  if (!isValidName(event.name)) return Result::InvalidName;
  if (find(normalized) != nullptr) return Result::InvalidEvent; // still active
  if (lastSequence_ == UINT32_MAX || event.sequence != lastSequence_ + 1 ||
      event.epoch == 0) {
    return Result::InvalidEvent;
  }
  if (event.type == 'M') {
    if (!event.seconds || event.seconds >= event.epoch) {
      return Result::InvalidEvent;
    }
    lastSequence_ = event.sequence;
    if (event.epoch > lastEpoch_) lastEpoch_ = event.epoch; // max baseline
    return Result::Ok;
  }
  if (event.epoch < lastEpoch_) return Result::ClockBackwards;
  if (event.type == 'I') {
    if (event.seconds != 0) return Result::InvalidEvent;
  } else if (!event.seconds || event.seconds >= event.epoch) {
    return Result::InvalidEvent;
  }
  lastSequence_ = event.sequence;
  lastEpoch_ = event.epoch;
  return Result::Ok;
}

Result Attendance::previewRecovery(uint32_t epoch, Event& event) const {
  if (!epoch || lastSequence_ == UINT32_MAX) return Result::InvalidEvent;
  Event next;
  next.sequence = lastSequence_ + 1;
  next.epoch = epoch;
  memcpy(next.uid, "00000000", 9);
  next.type = 'R';
  next.seconds = lastEpoch_; // audit previous clock baseline, NOT worked seconds
  next.name[0] = '\0';       // recovery never carries an employee name
  next.nameRecorded = false;
  next.archived = false;
  event = next;
  return Result::Ok;
}

Result Attendance::previewManual(const char* uid, uint32_t start, uint32_t end, Event& event) const {
  const Employee* employee = find(uid); // immutable historical identity, not current card
  if (!employee) return Result::UnknownCard;
  if (!start || end <= start || lastSequence_ == UINT32_MAX ||
      UINT64_MAX - employee->totalSeconds < end-start) return Result::InvalidEvent;
  if (employee->inside) return Result::OpenSession;
  Event next;
  next.sequence = lastSequence_ + 1;
  next.epoch = end;
  memcpy(next.uid, employee->uid, strlen(employee->uid) + 1);
  next.type = 'M';
  next.seconds = end-start;
  captureName(next, employee->name);
  event = next;
  return Result::Ok;
}

const Employee* Attendance::findByCard(const char* uid) const {
  char canonical[21];
  if (!normalizeUid(uid, canonical)) return nullptr;
  for (size_t i = 0; i < count_; ++i)
    if (strcmp(employees_[i].cardUid, canonical) == 0) return &employees_[i];
  return nullptr;
}

uint32_t Attendance::rosterVersion() const {
  uint32_t hash = 2166136261UL;
  for (size_t i = 0; i < count_; ++i) {
    const char* fields[] = {employees_[i].uid,employees_[i].name,employees_[i].cardUid};
    for (const char* text : fields) {
      do { hash = (hash ^ static_cast<uint8_t>(*text)) * 16777619UL; } while (*text++);
    }
    hash = (hash ^ static_cast<uint8_t>(employees_[i].active)) * 16777619UL;
  }
  return hash;
}

const Employee* Attendance::find(const char* uid) const {
  char normalized[UID_CAPACITY];
  if (!normalizeUid(uid, normalized)) {
    return nullptr;
  }
  for (size_t i = 0; i < count_; ++i) {
    if (strcmp(employees_[i].uid, normalized) == 0) {
      return &employees_[i];
    }
  }
  return nullptr;
}

const Employee* Attendance::at(size_t index) const {
  if (index >= count_) {
    return nullptr;
  }
  return &employees_[index];
}

bool Attendance::anyInside() const {
  for (size_t i = 0; i < count_; ++i) {
    if (employees_[i].inside) {
      return true;
    }
  }
  return false;
}

const char* resultText(Result result) {
  switch (result) {
    case Result::Ok:
      return "Basarili";
    case Result::InvalidUid:
      return "Gecersiz UID";
    case Result::InvalidName:
      return "Gecersiz isim";
    case Result::Full:
      return "Kayit dolu";
    case Result::UnknownCard:
      return "Bilinmeyen kart";
    case Result::TooSoon:
      return "Cok sik tarama";
    case Result::ClockBackwards:
      return "Saat geri alindi";
    case Result::InvalidEvent:
      return "Gecersiz olay";
    case Result::CardInUse: return "Kart baska personele ait";
    case Result::InactiveCard: return "Personel pasif";
    case Result::OpenSession: return "Once acik oturumu kapatin veya kurtarin";
  }
  return "Bilinmeyen sonuc";
}

}  // namespace pdks
