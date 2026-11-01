#pragma once
#include <ESP8266WebServer.h>
namespace pdks {
class Attendance;
namespace ota {
void setup(ESP8266WebServer& server, Attendance& core, const char* csrf, const bool* storageHealthy);
bool busy();
void tick();
}
}
