#pragma once
#include <stddef.h>
#include <stdint.h>
extern uint32_t mockNow;
inline uint32_t millis() { return mockNow; }
