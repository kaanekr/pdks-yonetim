#pragma once
// ---------------------------------------------------------------------------
// PDKS signed-OTA policy (pure C++11, no Arduino / no crypto / no state).
//
// These guards decide whether a signed firmware envelope may be offered and
// whether it matches this device. They are shared by the device upload path
// and by the host-side native runner (tools/test_ota_policy.py), so they must
// stay deterministic, allocation free and safe to call with hostile input.
//
// Canonical signed metadata (ASCII, LF, trailing LF, no CR, no spaces):
//   PDKS-FW1\n<target>\n<version>\n<sequence>\n<size>\n<sha256-hex>\n
// ---------------------------------------------------------------------------

#include <stddef.h>
#include <stdint.h>

namespace pdks {
namespace ota_policy {

constexpr size_t SIGNATURE_BYTES = 256;      // RSA-2048 raw signature
constexpr size_t SIGNATURE_B64_LEN = 344;    // 256 bytes -> 342 chars + "=="
constexpr size_t SHA256_HEX_LEN = 64;
constexpr uint32_t MIN_SIGNED_SIZE = 261;    // signed blob floor (raw bin + sig + len)
constexpr uint32_t MAX_SIGNED_SIZE = 1048576;
constexpr size_t ENVELOPE_MAX = 128;

// Strict "<major>.<minor>.<patch>": exactly three parts, 0..99 each, digits
// only and no leading zero. sequence = major*10000 + minor*100 + patch.
inline bool versionSequence(const char* text, uint32_t& sequence) {
  if (!text) return false;
  uint32_t parts[3] = {0, 0, 0};
  size_t part = 0;
  size_t digits = 0;
  for (const char* p = text;; ++p) {
    const char c = *p;
    if (c >= '0' && c <= '9') {
      if (digits == 0 && c == '0' && p[1] >= '0' && p[1] <= '9') return false;
      if (digits >= 2) return false;
      parts[part] = parts[part] * 10u + (uint32_t)(c - '0');
      ++digits;
      continue;
    }
    if (digits == 0) return false;
    if (c == '\0') {
      if (part != 2) return false;
      sequence = parts[0] * 10000u + parts[1] * 100u + parts[2];
      return true;
    }
    if (c != '.' || part == 2) return false;
    ++part;
    digits = 0;
  }
}

// Exactly `length` lowercase hex characters (never uppercase, never spaces).
inline bool lowerHex(const char* text, size_t length) {
  if (!text || length == 0) return false;
  for (size_t i = 0; i < length; ++i) {
    const char c = text[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  }
  return true;
}

inline int base64Value(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}

// Strict canonical base64 for a 256-byte RSA signature: exactly 344 chars,
// ends with "==", alphabet only elsewhere and zero pad bits in the last data
// char. Anything else (whitespace, missing pad, URL-safe alphabet) is refused.
inline bool decodeSignature(const char* base64, uint8_t out[SIGNATURE_BYTES]) {
  if (!base64 || !out) return false;
  size_t length = 0;
  while (base64[length] != '\0') {
    if (length >= SIGNATURE_B64_LEN) return false;
    ++length;
  }
  if (length != SIGNATURE_B64_LEN) return false;
  for (size_t i = 0; i + 2 < length; ++i) {
    if (base64Value(base64[i]) < 0) return false;
  }
  if (base64[length - 2] != '=' || base64[length - 1] != '=') return false;
  // Canonical pad bits must be zero; checked up front so a refused signature
  // never leaves a partially decoded buffer behind.
  const int padValue = base64Value(base64[length - 3]);
  if (padValue < 0 || (padValue & 0x0F) != 0) return false;
  size_t used = 0;
  for (size_t i = 0; i < length; i += 4) {
    const bool tail = (i + 4 == length);
    const int v0 = base64Value(base64[i]);
    const int v1 = base64Value(base64[i + 1]);
    const int v2 = tail ? 0 : base64Value(base64[i + 2]);
    const int v3 = tail ? 0 : base64Value(base64[i + 3]);
    if (v0 < 0 || v1 < 0 || v2 < 0 || v3 < 0) return false;
    if (tail) {
      if ((v1 & 0x0F) != 0) return false;  // non-canonical pad bits
      if (used >= SIGNATURE_BYTES) return false;
      out[used++] = (uint8_t)((v0 << 2) | (v1 >> 4));
      continue;
    }
    if (used + 3 > SIGNATURE_BYTES) return false;
    const uint32_t block = ((uint32_t)v0 << 18) | ((uint32_t)v1 << 12) |
                           ((uint32_t)v2 << 6) | (uint32_t)v3;
    out[used++] = (uint8_t)(block >> 16);
    out[used++] = (uint8_t)(block >> 8);
    out[used++] = (uint8_t)block;
  }
  return used == SIGNATURE_BYTES;
}

// Only builds this exact board; unknown targets are never guessed.
inline bool targetKnown(const char* target) {
  const char* const known = "nodemcuv2_sd_soft_tx";
  if (!target) return false;
  for (size_t i = 0;; ++i) {
    if (target[i] != known[i]) return false;
    if (known[i] == '\0') return true;
  }
}

inline size_t decimalDigits(uint32_t value) {
  size_t digits = 1;
  while (value >= 10) {
    value /= 10;
    ++digits;
  }
  return digits;
}

inline size_t writeDecimal(char* out, size_t used, uint32_t value) {
  const size_t digits = decimalDigits(value);
  for (size_t i = 0; i < digits; ++i) {
    out[used + digits - 1 - i] = (char)('0' + (value % 10u));
    value /= 10u;
  }
  return used + digits;
}

inline size_t writeText(char* out, size_t used, const char* text, size_t length) {
  for (size_t i = 0; i < length; ++i) out[used + i] = text[i];
  return used + length;
}

inline size_t textLength(const char* text) {
  size_t n = 0;
  while (text[n] != '\0') ++n;
  return n;
}

// Builds the canonical signed-metadata bytes. Returns 0 (and writes nothing
// usable) unless every field is valid AND the version derives `sequence`.
inline size_t envelope(char* out, size_t cap, const char* target, const char* version,
                       uint32_t sequence, uint32_t size, const char* sha256Hex) {
  if (!out || cap == 0 || !sha256Hex) return 0;
  if (!targetKnown(target)) return 0;
  uint32_t derived = 0;
  if (!versionSequence(version, derived) || derived != sequence) return 0;
  if (size < MIN_SIGNED_SIZE || size > MAX_SIGNED_SIZE) return 0;
  size_t shaLength = 0;
  while (shaLength <= SHA256_HEX_LEN && sha256Hex[shaLength] != '\0') ++shaLength;
  if (shaLength != SHA256_HEX_LEN || !lowerHex(sha256Hex, SHA256_HEX_LEN)) return 0;
  const size_t targetLength = textLength(target);
  const size_t versionLength = textLength(version);
  const size_t need = 9 + targetLength + 1 + versionLength + 1 +
                      decimalDigits(sequence) + 1 + decimalDigits(size) + 1 +
                      SHA256_HEX_LEN + 1;
  if (need > ENVELOPE_MAX || cap < need + 1) return 0;
  size_t used = 0;
  used = writeText(out, used, "PDKS-FW1\n", 9);
  used = writeText(out, used, target, targetLength);
  used = writeText(out, used, "\n", 1);
  used = writeText(out, used, version, versionLength);
  used = writeText(out, used, "\n", 1);
  used = writeDecimal(out, used, sequence);
  used = writeText(out, used, "\n", 1);
  used = writeDecimal(out, used, size);
  used = writeText(out, used, "\n", 1);
  used = writeText(out, used, sha256Hex, SHA256_HEX_LEN);
  used = writeText(out, used, "\n", 1);
  out[used] = '\0';
  return used;
}

// Cheap pre-flight: credentials + CSRF + healthy unlocked storage + nobody
// inside, and a strictly newer signed sequence than the running firmware.
inline bool mayPrepare(bool auth, bool csrf, bool storageHealthy, bool anyoneInside,
                       uint32_t currentSequence, uint32_t offeredSequence) {
  return auth && csrf && storageHealthy && !anyoneInside && offeredSequence > currentSequence;
}

}  // namespace ota_policy
}  // namespace pdks
