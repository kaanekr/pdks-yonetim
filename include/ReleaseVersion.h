#pragma once
#ifndef PDKS_FIRMWARE_VERSION
#define PDKS_FIRMWARE_VERSION "1.5.0"
#endif
#ifndef PDKS_FIRMWARE_SEQUENCE
#define PDKS_FIRMWARE_SEQUENCE 10500UL
#endif
#ifndef PDKS_PUBLIC_RELEASE
#define PDKS_PUBLIC_RELEASE 0
#endif
#ifndef PDKS_ENABLE_OTA
#define PDKS_ENABLE_OTA 0
#endif
#define PDKS_VERSION_STRING_INNER(x) #x
#define PDKS_VERSION_STRING(x) PDKS_VERSION_STRING_INNER(x)
namespace pdks {
namespace release {
constexpr const char* VERSION = PDKS_FIRMWARE_VERSION;
constexpr unsigned long SEQUENCE = PDKS_FIRMWARE_SEQUENCE;
constexpr const char* TARGET = "nodemcuv2_sd_soft_tx";
constexpr const char* BUILD = "PDKS-FW:" PDKS_FIRMWARE_VERSION ":" PDKS_VERSION_STRING(PDKS_FIRMWARE_SEQUENCE) ":nodemcuv2_sd_soft_tx";
}
}
