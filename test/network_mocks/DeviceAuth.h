#pragma once
#include "Config.h"
namespace pdks { namespace deviceauth {
inline bool ready() { return true; }
inline const char* apSsid() { return cfg::AP_SSID; }
inline const char* apPassword() { return cfg::AP_PASSWORD; }
} }
