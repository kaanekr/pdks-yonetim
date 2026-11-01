#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string>
class String {
 public:
  void reserve(size_t n) { value_.reserve(n); }
  String& operator+=(const char* s) { value_ += s; return *this; }
  String& operator+=(char c) { value_ += c; return *this; }
  size_t length() const { return value_.size(); }
  const char* c_str() const { return value_.c_str(); }
 private:
  std::string value_;
};
inline void yield() {}
