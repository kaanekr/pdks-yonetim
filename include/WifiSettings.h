#pragma once
#include <stddef.h>
#include <stdint.h>

namespace pdks {
namespace network {
constexpr size_t SETTINGS_MAX = 512;
struct Settings {
  bool enabled = false;
  char ssid[33] = {};
  char password[65] = {};
};
bool valid(const Settings& settings);
bool decode(const char* json, size_t length, Settings& out);
size_t encode(const Settings& settings, char* json, size_t capacity);
} // namespace network
} // namespace pdks
