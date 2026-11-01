// Production core AND storage compiled against in-memory file I/O, no device.
#include <cstdio>
#include <cstring>
#include "LittleFS.h"
namespace storemock {
std::map<std::string,std::string> files;
bool failWrite=false,failRead=false,failRename=false;
int formats=0;
}
MockFS LittleFS;
#include "../src/Storage.cpp"
using namespace pdks;
static int checks=0,failures=0;
#define CHECK(x) do { ++checks; if(!(x)) { ++failures; std::printf("FAIL %d: %s\n",__LINE__,#x); } } while(0)
static void boot() {
  storage::g_mounted=false; storage::g_locked=false; storage::g_ready=false;
  storage::g_haveSeq=false; storage::g_lastSeq=0; storage::g_lastEpoch=0; storage::g_lines=0;
  storemock::failWrite=storemock::failRead=storemock::failRename=false;
  CHECK(storage::mount());
}
static bool replay(const Event& e,void* core) { return static_cast<Attendance*>(core)->commit(e)==Result::Ok; }
static void appendScan(Attendance& core,const char* uid,uint32_t epoch) {
  Event e; CHECK(core.preview(uid,epoch,e)==Result::Ok);
  CHECK(storage::appendEvent(e)==storage::Status::Ok); CHECK(core.commit(e)==Result::Ok);
}
int main() {
  const uint32_t t=1800000000UL;
  storemock::files["/employees.json"]="{\"v\":1,\"employees\":[{\"uid\":\"11223344\",\"name\":\"Deneme\"}]}";
  storemock::files["/wifi.json"]="wifi-sentinel";
  storemock::files["/index.html"]="panel-sentinel";
  boot(); Attendance core;
  CHECK(storage::loadEmployees(core)==storage::Status::Ok);
  CHECK(core.find("11223344")->active);
  CHECK(strcmp(core.find("11223344")->cardUid,"11223344")==0);
  CHECK(storage::replayJournal(replay,&core)==storage::Status::Ok);
  appendScan(core,"11223344",t);
  appendScan(core,"11223344",t+100);
  appendScan(core,"11223344",t+110);
  CHECK(core.find("11223344")->totalSeconds==100 && core.anyInside());
  CHECK(core.updateEmployee("11223344","Rename while inside","11223344",true)==Result::Ok);
  CHECK(core.find("11223344")->inside && core.find("11223344")->entryEpoch==t+110);
  const std::string originalLog=storemock::files["/journal.log"];
  const uint32_t version=core.rosterVersion();
  CHECK(core.updateEmployee("11223344","Yeni ad","AABBCCDD",true)==Result::OpenSession);
  CHECK(core.updateEmployee("11223344","Yeni ad","11223344",false)==Result::OpenSession);
  CHECK(core.rosterVersion()==version);
  Event r; CHECK(core.previewRecovery(t-1000,r)==Result::Ok);
  CHECK(r.type=='R' && r.seconds==t+110);
  CHECK(core.anyInside()); // preview is non-mutating
  CHECK(storage::appendEvent(r)==storage::Status::Ok);
  // Power loss after durable R and BEFORE live commit/RTC programming:
  boot(); Attendance powerLoss;
  CHECK(storage::loadEmployees(powerLoss)==storage::Status::Ok);
  CHECK(storage::replayJournal(replay,&powerLoss)==storage::Status::Ok);
  CHECK(!powerLoss.anyInside() && powerLoss.lastEpoch()==t-1000);
  CHECK(powerLoss.find("11223344")->totalSeconds==100);
  CHECK(core.commit(r)==Result::Ok);
  CHECK(!core.anyInside() && core.lastEpoch()==t-1000);
  CHECK(core.find("11223344")->totalSeconds==100);
  CHECK(storemock::files["/journal.log"].substr(0,originalLog.size())==originalLog);
  CHECK(core.updateEmployee("11223344","Yeni ad","AABBCCDD",false)==Result::Ok);
  CHECK(core.rosterVersion()!=version);
  CHECK(storage::saveEmployees(core,nullptr)==storage::Status::Ok);
  CHECK(storemock::files["/employees.json"].find("\"v\":2")!=std::string::npos);
  boot(); Attendance reloaded;
  CHECK(storage::loadEmployees(reloaded)==storage::Status::Ok);
  CHECK(storage::replayJournal(replay,&reloaded)==storage::Status::Ok);
  CHECK(reloaded.find("11223344")->totalSeconds==100 && !reloaded.anyInside());
  CHECK(!reloaded.find("11223344")->active);
  Event e; CHECK(reloaded.preview("AABBCCDD",t-900,e)==Result::InactiveCard);
  CHECK(reloaded.preview("11223344",t-900,e)==Result::UnknownCard);
  CHECK(reloaded.updateEmployee("11223344","Yeni ad","AABBCCDD",true)==Result::Ok);
  CHECK(reloaded.preview("AABBCCDD",t-900,e)==Result::Ok);
  CHECK(strcmp(e.uid,"11223344")==0); // event uses stable identity, not replacement card
  CHECK(reloaded.upsertEmployee("AABBCCDD","Clash")==Result::CardInUse);
  appendScan(reloaded,"AABBCCDD",t-900);
  CHECK(strcmp(reloaded.find("11223344")->cardUid,"AABBCCDD")==0);
  CHECK(storemock::files["/journal.log"].find("11223344,I,0")!=std::string::npos);
  CHECK(reloaded.find("11223344")->totalSeconds==100);
  CHECK(reloaded.previewRecovery(t-2000,r)==Result::Ok);
  Event bad=r; ++bad.seconds;
  CHECK(storage::appendEvent(bad)==storage::Status::BadData);
  CHECK(reloaded.commit(bad)==Result::InvalidEvent && reloaded.anyInside());
  const std::string roster=storemock::files["/employees.json"];
  storemock::failWrite=true;
  CHECK(storage::saveEmployees(reloaded,nullptr)==storage::Status::IoError);
  CHECK(storemock::files["/employees.json"]==roster);
  storemock::failWrite=false; storemock::failRename=true;
  CHECK(storage::saveEmployees(reloaded,nullptr)==storage::Status::IoError);
  CHECK(storemock::files["/employees.json"]==roster);
  storemock::failRename=false; storemock::failWrite=true;
  CHECK(storage::appendEvent(r)==storage::Status::IoError);
  CHECK(storage::locked() && reloaded.anyInside()); // never commit failed append
  boot(); Attendance afterFault;
  CHECK(storage::loadEmployees(afterFault)==storage::Status::Ok);
  CHECK(storage::replayJournal(replay,&afterFault)==storage::Status::IntegrityError);
  CHECK(storage::locked());
  CHECK(storemock::files["/wifi.json"]=="wifi-sentinel");
  CHECK(storemock::files["/index.html"]=="panel-sentinel");
  CHECK(storemock::formats==0);
  // Strict v2 roster validation; legacy v1 was tested above.
  const char* invalidRosters[] = {
    "{\"v\":2,\"employees\":[{\"uid\":\"11223344\",\"name\":\"A\"}]}",
    "{\"v\":2,\"employees\":[{\"uid\":\"11223344\",\"name\":\"A\",\"card_uid\":\"AABBCCDD\",\"active\":\"false\"}]}",
    "{\"v\":2,\"employees\":[{\"uid\":\"11223344\",\"name\":\"A\",\"card_uid\":\"aabbccdd\",\"active\":true}]}",
    "{\"v\":3,\"employees\":[]}"
  };
  for(const char* text:invalidRosters) {
    storemock::files["/employees.json"]=text;
    boot(); Attendance invalid;
    CHECK(storage::loadEmployees(invalid)==storage::Status::IntegrityError);
    CHECK(storage::locked());
  }
  boot(); Attendance full;
  for(unsigned i=1;i<=32;++i) {
    char uid[21]; std::snprintf(uid,sizeof(uid),"%08X",i);
    CHECK(full.upsertEmployee(uid,"123456789012345678901234567890123456789012345678")==Result::Ok);
  }
  CHECK(storage::saveEmployees(full,nullptr)==storage::Status::Ok);
  boot(); Attendance fullReload;
  CHECK(storage::loadEmployees(fullReload)==storage::Status::Ok);
  CHECK(fullReload.count()==32);
  CHECK(storemock::files["/wifi.json"]=="wifi-sentinel" && storemock::formats==0);
  std::printf("management/storage: %d checks, %d failure(s)\n",checks,failures);
  return failures?1:0;
}
