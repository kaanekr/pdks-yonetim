#pragma once
#include <string>
#include <stdint.h>
enum { WIFI_AP_STA = 3, WL_DISCONNECTED = 6, WL_CONNECTED = 3 };
struct IPAddress {
  uint8_t bytes[4];
  IPAddress(uint8_t a, uint8_t b, uint8_t c, uint8_t d) : bytes{a,b,c,d} {}
};
struct MockWiFi {
  bool persistentValue = true, autoReconnect = true, ap = false, everOff = false;
  int modeValue = 0, begins = 0, disconnects = 0, state = WL_DISCONNECTED;
  int apStarts = 0, apStops = 0;
  std::string ssid, password;
  void persistent(bool value) { persistentValue = value; }
  void mode(int value) { modeValue = value; }
  void setAutoReconnect(bool value) { autoReconnect = value; }
  void disconnect(bool off) { ++disconnects; everOff |= off; state = WL_DISCONNECTED; }
  bool softAPConfig(IPAddress, IPAddress, IPAddress) { modeValue |= 2; return true; }
  bool softAP(const char*, const char*) { ap = true; ++apStarts; modeValue |= 2; return true; }
  bool softAPdisconnect(bool off) {
    if (off) { ap = false; modeValue &= ~2; ++apStops; }
    return true;
  }
  void begin(const char* name, const char* pass) { ++begins; ssid = name; password = pass; }
  int status() const { return state; }
};
extern MockWiFi WiFi;
