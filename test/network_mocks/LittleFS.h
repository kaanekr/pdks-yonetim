#pragma once
#include <stdint.h>
#include <stddef.h>
#include <map>
#include <string>
#include <string.h>

namespace netmock {
extern std::map<std::string, std::string> files;
extern bool failWrite, failRead, failRename;
}
class File {
 public:
  File() : ok_(false) {}
  File(const char* path, bool write) : path_(path), ok_(true) {
    if (write) netmock::files[path_].clear();
    else if (!netmock::files.count(path_)) ok_ = false;
  }
  explicit operator bool() const { return ok_; }
  size_t size() const { return ok_ ? netmock::files[path_].size() : 0; }
  size_t readBytes(char* out, size_t len) {
    if (!ok_ || netmock::failRead) return 0;
    const std::string& data = netmock::files[path_];
    if (len > data.size()) len = data.size();
    memcpy(out, data.data(), len); return len;
  }
  size_t write(const uint8_t* data, size_t len) {
    if (!ok_) return 0;
    if (netmock::failWrite && len) --len;
    netmock::files[path_].assign(reinterpret_cast<const char*>(data), len); return len;
  }
  void flush() {}
  void close() { ok_ = false; }
 private:
  std::string path_;
  bool ok_;
};
struct MockFS {
  File open(const char* path, const char* mode) { return File(path, mode[0] == 'w'); }
  bool exists(const char* path) const { return netmock::files.count(path) != 0; }
  bool rename(const char* from, const char* to) {
    if (netmock::failRename || !exists(from)) return false;
    netmock::files[to] = netmock::files[from]; netmock::files.erase(from); return true;
  }
};
extern MockFS LittleFS;
