#pragma once
namespace pdks { namespace deviceauth {
// Existing per-device credentials are migrated only by a private USB bootstrap.
// Public OTA images contain no fallback credential and refuse missing/bad files.
bool begin();
bool ready();
const char* username();
const char* password();
const char* apSsid();
const char* apPassword();
} }
