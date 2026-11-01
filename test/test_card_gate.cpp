#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <vector>

#include "Attendance.h"
#include "CardGate.h"
#include "Config.h"

static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; \
  printf("FAIL line %d: %s\n", __LINE__, #x); } } while (0)

using byte = uint8_t;
static uint32_t g_now = 0;
static uint32_t millis() { return g_now; }
using pdks::Attendance;

// Hardware mock distinguishes a HALTed held card from actual RF absence.
struct MFRC522 {
  enum StatusCode { STATUS_OK, STATUS_TIMEOUT, STATUS_COLLISION, STATUS_ERROR };
  enum Register { TxModeReg, RxModeReg, ModWidthReg, VersionReg };
  struct Uid { byte size = 4; byte uidByte[10] = {0xDE, 0xAD, 0xBE, 0xEF}; } uid;
  bool present = true, halted = false, serialOk = true;
  byte version = 0x92;
  StatusCode forced = STATUS_OK;
  int wakes = 0, selects = 0, halts = 0, stops = 0, registerWrites = 0;
  void PCD_WriteRegister(Register reg, byte value) {
    CHECK((reg == TxModeReg && value == 0) || (reg == RxModeReg && value == 0) ||
          (reg == ModWidthReg && value == 0x26));
    ++registerWrites;
  }
  byte PCD_ReadRegister(Register reg) { CHECK(reg == VersionReg); return version; }
  StatusCode PICC_WakeupA(byte* atqa, byte* size) {
    ++wakes; CHECK(*size == 2);
    if (forced != STATUS_OK) return forced;
    if (!present) return STATUS_TIMEOUT;
    halted = false; atqa[0] = 4; atqa[1] = 0; return STATUS_OK;
  }
  bool PICC_ReadCardSerial() { ++selects; return serialOk && present; }
  void PICC_HaltA() { ++halts; halted = true; }
  void PCD_StopCrypto1() { ++stops; }
};
static MFRC522 g_rfid;
static bool g_rfidOk = true;
static pdks::CardGate g_cardGate(cfg::RFID_RELEASE_MS, cfg::RFID_RELEASE_SAMPLES);
static Attendance core;
static int attempts = 0, records = 0, warnings = 0;
static std::vector<char> eventTypes;
static void handleCard(const char* uid) {
  ++attempts;
  pdks::Event event;
  pdks::Result result = core.preview(uid, cfg::MIN_EPOCH + g_now / 1000, event);
  if (result == pdks::Result::Ok) {
    CHECK(core.commit(event) == pdks::Result::Ok);
    ++records; eventTypes.push_back(event.type);
  } else {
    ++warnings;
  }
}

// Generated verbatim from main.cpp by tools/test_card_gate.py.
#include "rfid_scan.inc"

static void poll(uint32_t now) { g_now = now; scanRfid(); }
static void removeFor(uint32_t from) {
  g_rfid.present = false;
  for (uint32_t delta = 0; delta <= 900; delta += 180) poll(from + delta);
  g_rfid.present = true;
}

static void testGate() {
  using pdks::CardPresence;
  pdks::CardGate gate(600, 3);
  const char* uid = "DEADBEEF";
  CHECK(!gate.observe(CardPresence::Uncertain, uid, 0));
  CHECK(!gate.observe(CardPresence::Present, nullptr, 0));
  CHECK(!gate.observe(CardPresence::Present, "", 0));
  CHECK(!gate.observe(CardPresence::Present, "012345678901234567890", 0));
  CHECK(gate.observe(CardPresence::Present, uid, 0));
  for (uint32_t now = 180; now < 30000; now += 180)
    CHECK(!gate.observe(CardPresence::Present, uid, now));
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 30000));
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 30180));
  CHECK(!gate.observe(CardPresence::Present, uid, 30360)); // missed reads reset
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 31000));
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 31300));
  CHECK(!gate.observe(CardPresence::Uncertain, nullptr, 31600)); // errors cannot rearm
  CHECK(!gate.observe(CardPresence::Present, uid, 32000));
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 33000));
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 33300));
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 33600));
  CHECK(gate.observe(CardPresence::Present, uid, 33700));
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 0xFFFFFE00u));
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 0xFFFFFF00u));
  CHECK(!gate.observe(CardPresence::Absent, nullptr, 0x00000100u));
  CHECK(gate.observe(CardPresence::Present, uid, 0x00000200u));
  // Repeated polls at one timestamp cannot substitute for elapsed absence.
  for (int i = 0; i < 300; ++i) CHECK(!gate.observe(CardPresence::Absent, nullptr, 1000));
  CHECK(!gate.observe(CardPresence::Present, uid, 1000));
  // No empty-field delay is imposed on another UID, including longer cards.
  CHECK(gate.observe(CardPresence::Present, "0123456789ABCD", 1001));
  CHECK(!gate.observe(CardPresence::Present, "0123456789ABCD", 1002));
  CHECK(gate.observe(CardPresence::Present, "0123456789ABCDEF0123", 1003));
  CHECK(!gate.observe(CardPresence::Present, "0123456789ABCDEF0123", 1004));
  CHECK(gate.observe(CardPresence::Present, uid, 1005));
  CHECK(!gate.observe(CardPresence::Present, uid, 1006));
}

int main() {
  testGate();
  CHECK(core.upsertEmployee("DEADBEEF", "Umut") == pdks::Result::Ok);
  poll(1000); CHECK(attempts == 1); CHECK(records == 1); CHECK(eventTypes[0] == 'I');
  for (uint32_t now = 1180; now <= 21000; now += 180) {
    g_rfid.present = (((now - 1180) / 180) % 5 != 0); // isolated RF misses
    poll(now);
    CHECK(attempts == 1); CHECK(records == 1); CHECK(warnings == 0);
  }
  g_rfid.present = true;
  g_rfid.forced = MFRC522::STATUS_ERROR;
  for (uint32_t now = 22000; now < 24000; now += 180) poll(now);
  g_rfid.forced = MFRC522::STATUS_OK;
  poll(24000); CHECK(attempts == 1);
  // An unplugged reader/0xFF version must not look like card removal.
  g_rfid.present = false; g_rfid.version = 0xFF;
  poll(25000); CHECK(!g_rfidOk);
  poll(27000);
  g_rfidOk = true; g_rfid.version = 0x92; g_rfid.present = true;
  poll(28000); CHECK(attempts == 1);
  removeFor(30000);
  poll(31000); CHECK(attempts == 2); CHECK(records == 2); CHECK(eventTypes[1] == 'O');
  removeFor(32000);
  poll(33000); CHECK(attempts == 3); CHECK(records == 2); CHECK(warnings == 1);
  for (uint32_t now = 33180; now <= 48000; now += 180) poll(now);
  CHECK(attempts == 3); CHECK(records == 2); CHECK(warnings == 1);
  // A selection failure is presence, not removal, even after many seconds.
  g_rfid.serialOk = false;
  for (uint32_t now = 49000; now <= 51000; now += 180) poll(now);
  g_rfid.serialOk = true;
  poll(52000); CHECK(attempts == 3);
  removeFor(53000); poll(54000);
  CHECK(attempts == 4); CHECK(records == 3); CHECK(eventTypes[2] == 'I');
  // Unknown cards also get only one warning per presentation.
  removeFor(55000); g_rfid.uid.uidByte[0] = 0xAA;
  poll(56000);
  const int unknownAttempts = attempts, unknownWarnings = warnings;
  for (uint32_t now = 56180; now < 70000; now += 180) poll(now);
  CHECK(attempts == unknownAttempts); CHECK(warnings == unknownWarnings);
  CHECK(g_rfid.halted); CHECK(g_rfid.wakes > 200);
  CHECK(g_rfid.halts > 100); CHECK(g_rfid.registerWrites == g_rfid.wakes * 3);
  // Real scan adapter: back-to-back different people are accepted immediately,
  // while returning to A/B too early still uses each person's own 5s timer.
  CHECK(core.upsertEmployee("B1ADBEEF", "Veli") == pdks::Result::Ok);
  CHECK(core.upsertEmployee("C1ADBEEF", "Ayse") == pdks::Result::Ok);
  const int beforeAttempts = attempts, beforeRecords = records, beforeWarnings = warnings;
  g_rfid.uid.uidByte[0] = 0xDE; poll(71000); // A, no absent sample after unknown
  g_rfid.uid.uidByte[0] = 0xB1; poll(71180); // B immediately after A
  g_rfid.uid.uidByte[0] = 0xC1; poll(71360); // C immediately after B
  CHECK(attempts == beforeAttempts + 3); CHECK(records == beforeRecords + 3);
  CHECK(warnings == beforeWarnings);
  g_rfid.uid.uidByte[0] = 0xDE; poll(72000); // A has not waited 5s
  CHECK(records == beforeRecords + 3); CHECK(warnings == beforeWarnings + 1);
  poll(72180); CHECK(warnings == beforeWarnings + 1); // held A is silent
  g_rfid.uid.uidByte[0] = 0xB1; poll(73000); // B also has its own timer
  CHECK(records == beforeRecords + 3); CHECK(warnings == beforeWarnings + 2);
  g_rfid.uid.uidByte[0] = 0xC1; poll(74000);
  CHECK(warnings == beforeWarnings + 3);
  g_rfid.uid.uidByte[0] = 0xDE; poll(76000); // A exactly 5s after 71000
  g_rfid.uid.uidByte[0] = 0xB1; poll(76180); // B exactly 5s after 71180
  CHECK(records == beforeRecords + 5); CHECK(warnings == beforeWarnings + 3);
  printf("card presentation: %d checks, %d failure(s)\n", checks, failures);
  return failures ? 1 : 0;
}
