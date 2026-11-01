#pragma once

#include <stdint.h>
#include <string.h>

namespace pdks {

enum class CardPresence : uint8_t { Absent, Present, Uncertain };

// Suppress a held UID, not the entire reader. A different normalized UID is
// immediately a new presentation. Same-UID rearming needs confirmed absence;
// Attendance separately enforces the cooldown per employee.
class CardGate {
 public:
  CardGate(uint32_t releaseMs, uint8_t releaseSamples)
      : releaseMs_(releaseMs), releaseSamples_(releaseSamples) {}

  bool observe(CardPresence presence, const char* uid, uint32_t now) {
    if (presence != CardPresence::Absent) {
      missing_ = false;
      samples_ = 0;
      if (presence == CardPresence::Present && uid && uid[0] && strlen(uid) <= 20 &&
          (!latched_ || strcmp(uid, latchedUid_) != 0)) {
        strncpy(latchedUid_, uid, sizeof(latchedUid_) - 1);
        latchedUid_[sizeof(latchedUid_) - 1] = '\0';
        latched_ = true;
        return true;
      }
      return false;
    }
    if (!latched_) return false;
    if (!missing_) {
      missing_ = true;
      missingAt_ = now;
      samples_ = 1;
    } else if (samples_ < 255) {
      ++samples_;
    }
    if (samples_ >= releaseSamples_ && now - missingAt_ >= releaseMs_) {
      latched_ = false;
      latchedUid_[0] = '\0';
      missing_ = false;
      samples_ = 0;
    }
    return false;
  }

 private:
  const uint32_t releaseMs_;
  const uint8_t releaseSamples_;
  uint32_t missingAt_ = 0;
  uint8_t samples_ = 0;
  bool missing_ = false;
  bool latched_ = false;
  char latchedUid_[21] = {};
};

} // namespace pdks
