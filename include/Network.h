#pragma once
#include "WifiSettings.h"

namespace pdks {
namespace network {
void begin();
void tick(uint32_t now);
bool save(const Settings& settings);
const Settings& settings();
bool configOk();
bool connected();
bool setupApActive();
uint32_t setupApSecondsLeft();
const char* state();
} // namespace network
} // namespace pdks
