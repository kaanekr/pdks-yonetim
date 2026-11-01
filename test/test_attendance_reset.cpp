// Actual ResetIo adapter is inserted at RESET_IO below by the host test runner.
#include "Attendance.h"
#include "AttendanceReset.h"
#include <map>
#include <string>
#include <vector>
#include <stdio.h>
#include <string.h>

struct PowerCut {};
static std::map<std::string, std::string> files, sdFiles;
static std::vector<std::string> removed;
static int operation = 0, cutAt = -1;
static bool failWrite = false, failRead = false, failRename = false;
static bool failRemove = false, sdReady = true;
static std::string failOpen;
static void step() { if (operation++ == cutAt) throw PowerCut(); }

class File {
 public:
  File() : valid_(false) {}
  explicit File(const char* path) : path_(path), valid_(true) {}
  explicit operator bool() const { return valid_; }
  size_t size() const { step(); return files[path_].size(); }
  int read(uint8_t* out, size_t length) {
    step();
    if (!valid_ || failRead) return -1;
    if (length > files[path_].size()) length = files[path_].size();
    memcpy(out, files[path_].data(), length);
    step(); return static_cast<int>(length);
  }
  size_t write(const uint8_t* data, size_t length) {
    step();
    if (!valid_) return 0;
    if (failWrite && length) --length;
    files[path_].assign(reinterpret_cast<const char*>(data), length);
    step(); return length;
  }
  void flush() { step(); }
  void close() { step(); valid_ = false; }
 private:
  std::string path_;
  bool valid_;
};

struct MockFS {
  bool exists(const char* path) { step(); return files.count(path) != 0; }
  File open(const char* path, const char* mode) {
    step();
    if (failOpen == path) return File();
    if (mode[0] == 'w') files[path].clear();
    if (!files.count(path)) return File();
    step(); return File(path);
  }
  bool rename(const char* from, const char* to) {
    step();
    if (failRename || !files.count(from)) return false;
    files[to] = files[from]; files.erase(from);
    step(); return true;
  }
  bool remove(const char* path) {
    step();
    if (failRemove || !files.count(path)) return false;
    files.erase(path); removed.push_back(path);
    step(); return true;
  }
};
static MockFS LittleFS;
namespace pdks { namespace softsd {
bool resetEvents() {
  step();
  if (!sdReady) return false;
  sdFiles.erase("/events.log");
  step(); return true;
}
} }

// RESET_IO

static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; \
  printf("FAIL line %d: %s\n", __LINE__, #x); } } while (0)

static const char* ID = "2026102501";
static void seed() {
  files = {{"/employees.json", "synthetic roster"},
           {"/employees.tmp", "synthetic temporary roster"},
           {"/journal.log", "synthetic journal"},
           {"/wifi.json", "wifi sentinel"}, {"/wifi.tmp", "wifi temp sentinel"},
           {"/index.html", "panel sentinel"}, {"/other.txt", "other sentinel"}};
  sdFiles = {{"/events.log", "synthetic SD mirror"}, {"/other.csv", "SD sentinel"}};
  removed.clear(); operation = 0; cutAt = -1;
  failWrite = failRead = failRename = failRemove = false;
  sdReady = true; failOpen.clear();
}

static void preserved() {
  CHECK(files["/wifi.json"] == "wifi sentinel");
  CHECK(files["/wifi.tmp"] == "wifi temp sentinel");
  CHECK(files["/index.html"] == "panel sentinel");
  CHECK(files["/other.txt"] == "other sentinel");
  CHECK(sdFiles["/other.csv"] == "SD sentinel");
  for (const std::string& path : removed)
    CHECK(path == "/employees.json" || path == "/employees.tmp" || path == "/journal.log");
}

static void cleared() {
  CHECK(!files.count("/employees.json")); CHECK(!files.count("/employees.tmp"));
  CHECK(!files.count("/journal.log")); CHECK(!sdFiles.count("/events.log"));
  CHECK(!files.count(pdks::reset::PENDING));
  CHECK(files[pdks::reset::DONE] == ID);
  preserved();
}

int main() {
  using pdks::reset::Result;
  using pdks::reset::Marker;
  ResetIo io;
  seed(); CHECK(pdks::reset::run(io, ID) == Result::Cleared);
  const int completeOperations = operation;
  cleared();
  // A completed request must NEVER erase later enrolments/events on reboot.
  files["/employees.json"] = "new roster"; files["/journal.log"] = "new journal";
  sdFiles["/events.log"] = "new SD mirror";
  removed.clear();
  CHECK(pdks::reset::run(io, ID) == Result::AlreadyDone);
  CHECK(files["/employees.json"] == "new roster");
  CHECK(files["/journal.log"] == "new journal");
  CHECK(sdFiles["/events.log"] == "new SD mirror"); CHECK(removed.empty()); preserved();

  // Every primitive boundary can lose power, including after atomic rename.
  // Retrying the same request must finish safely without touching other files.
  for (int boundary = 0; boundary <= completeOperations; ++boundary) {
    seed(); cutAt = boundary;
    try { pdks::reset::run(io, ID); } catch (const PowerCut&) {}
    cutAt = -1; operation = 0;
    const Result result = pdks::reset::run(io, ID);
    CHECK(result == Result::Cleared || result == Result::AlreadyDone);
    cleared();
  }

  seed(); sdReady = false;
  CHECK(pdks::reset::run(io, ID) == Result::Failed);
  CHECK(files["/employees.json"] == "synthetic roster");
  CHECK(files["/journal.log"] == "synthetic journal"); CHECK(removed.empty());
  CHECK(files[pdks::reset::PENDING] == ID); preserved();
  sdReady = true; CHECK(pdks::reset::run(io, ID) == Result::Cleared); cleared();

  seed(); failWrite = true; CHECK(pdks::reset::run(io, ID) == Result::Failed);
  CHECK(removed.empty()); CHECK(!files.count(pdks::reset::PENDING)); preserved();
  seed(); failRead = true; CHECK(pdks::reset::run(io, ID) == Result::Failed);
  CHECK(removed.empty()); preserved();
  seed(); failRename = true; CHECK(pdks::reset::run(io, ID) == Result::Failed);
  CHECK(removed.empty()); preserved();
  seed(); failRemove = true; CHECK(pdks::reset::run(io, ID) == Result::Failed);
  CHECK(!files.count(pdks::reset::DONE)); CHECK(files[pdks::reset::PENDING] == ID);
  failRemove = false; CHECK(pdks::reset::run(io, ID) == Result::Cleared); cleared();
  seed(); failOpen = pdks::reset::TEMP;
  CHECK(pdks::reset::run(io, ID) == Result::Failed); CHECK(removed.empty()); preserved();

  // Corrupted or conflicting authorisation markers fail closed, not a new wipe.
  const char* badMarkers[] = {"", "garbage", "12345678901", "123\n", "2026102502"};
  for (const char* bad : badMarkers) {
    seed(); files[pdks::reset::PENDING] = bad;
    CHECK(pdks::reset::run(io, ID) == Result::Failed); CHECK(removed.empty()); preserved();
  }
  seed(); files[pdks::reset::DONE] = "corrupt";
  CHECK(pdks::reset::run(io, ID) == Result::Failed); CHECK(removed.empty()); preserved();
  seed(); files[pdks::reset::DONE] = ID; files[pdks::reset::PENDING] = ID;
  CHECK(pdks::reset::run(io, ID) == Result::Failed); CHECK(removed.empty());
  seed(); files[pdks::reset::DONE] = "2026102401";
  CHECK(pdks::reset::run(io, ID) == Result::Cleared); cleared();
  seed(); files[pdks::reset::PENDING] = std::string("2026\0x", 6);
  CHECK(io.marker(pdks::reset::PENDING, ID) == Marker::Invalid);
  CHECK(pdks::reset::run(io, ID) == Result::Failed); CHECK(removed.empty());
  for (const char* bad : {static_cast<const char*>(nullptr), "", "invalid", "12345678901"}) {
    seed(); CHECK(pdks::reset::run(io, bad) == Result::Failed);
    CHECK(operation == 0); CHECK(removed.empty());
  }

  // The normal core is empty after a fresh boot/replay of absent data files:
  // no open sessions or last-epoch barrier remains for the time-setting route.
  pdks::Attendance empty;
  CHECK(empty.count() == 0); CHECK(!empty.anyInside()); CHECK(empty.lastEpoch() == 0);
  printf("attendance maintenance reset: %d checks, %d failure(s); %d power-cut boundaries\n",
         checks, failures, completeOperations + 1);
  return failures ? 1 : 0;
}
