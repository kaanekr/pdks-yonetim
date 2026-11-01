"""Run the actual delete HTTP handlers with native auth/server/file stubs, no network."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT/'src/main.cpp').read_text(encoding='utf-8')
    guards = source[source.index('static bool currentManagementState('):source.index('static void handleManualAttendance(')]
    handlers = source[source.index('static void handlePermanentDelete('):source.index('static void writeRtcEpoch(')]
    harness = r'''
#include <cstdio>
#include <cstring>
#include <string>
#include "LittleFS.h"
namespace storemock {
std::map<std::string,std::string> files;
bool failWrite=false,failRead=false,failRename=false;
int formats=0;
}
MockFS LittleFS;
#include "src/Storage.cpp"
using namespace pdks;
static Attendance g_core;
static bool g_storageHealthy=true,auth=true,csrf=true,bodyAllowed=true;
static int code=0,checks=0,failures=0;
static bool replyOk=false;
struct FakeServer { std::string body; std::string arg(const char*) { return body; } } g_server;
static bool requireAuth() { return auth; }
static bool requireCsrf() { return csrf; }
static bool bodyWithinLimit() { return bodyAllowed; }
static void sendResult(int value,bool ok,const char*) { code=value; replyOk=ok; }
#define CHECK(x) do { ++checks; if(!(x)) { ++failures; std::printf("FAIL %d: %s\n",__LINE__,#x); } } while(0)
'''
    harness += guards + handlers
    harness += r'''
static bool replay(const Event& event,void*) { return g_core.commit(event)==Result::Ok; }
static void seed() {
  storemock::files.clear(); storemock::failWrite=storemock::failRead=storemock::failRename=false;
  storage::g_mounted=storage::g_locked=storage::g_ready=storage::g_haveSeq=false;
  storage::g_lastSeq=storage::g_lastEpoch=storage::g_lines=0;
  g_core.clear(); auth=csrf=bodyAllowed=g_storageHealthy=true;
  storemock::files["/employees.json"]="{\"v\":2,\"employees\":[{\"uid\":\"11223344\",\"name\":\"Ali\",\"card_uid\":\"11223344\",\"active\":true},{\"uid\":\"AABBCCDD\",\"name\":\"Ayse\",\"card_uid\":\"AABBCCDD\",\"active\":true}]}";
  storemock::files["/wifi.json"]="wifi-sentinel";
  storemock::files["/index.html"]="html-sentinel";
  CHECK(storage::mount());
  CHECK(storage::loadEmployees(g_core)==storage::Status::Ok);
  CHECK(storage::replayJournal(replay,nullptr)==storage::Status::Ok);
  const char* people[]={"11223344","11223344","AABBCCDD"};
  for(unsigned i=0;i<3;++i) {
    Event event; CHECK(g_core.preview(people[i],1800000000UL+i*100,event)==Result::Ok);
    CHECK(storage::appendEvent(event)==storage::Status::Ok); CHECK(g_core.commit(event)==Result::Ok);
  }
  Event recovery; CHECK(g_core.previewRecovery(1800000300UL,recovery)==Result::Ok);
  CHECK(storage::appendEvent(recovery)==storage::Status::Ok); CHECK(g_core.commit(recovery)==Result::Ok);
}
static std::string request(bool person,unsigned target=1) {
  char body[512];
  if(person) std::snprintf(body,sizeof(body),"{\"confirm\":\"PERSONELI KALICI SIL\",\"uid\":\"AABBCCDD\",\"expected_sequence\":%lu,\"roster_version\":%lu,\"journal_revision\":%lu}",
      (unsigned long)g_core.lastSequence(),(unsigned long)g_core.rosterVersion(),(unsigned long)storage::journalRevision());
  else std::snprintf(body,sizeof(body),"{\"confirm\":\"OTURUMU KALICI SIL\",\"target_sequence\":%u,\"expected_sequence\":%lu,\"roster_version\":%lu,\"journal_revision\":%lu}",
      target,(unsigned long)g_core.lastSequence(),(unsigned long)g_core.rosterVersion(),(unsigned long)storage::journalRevision());
  return body;
}
static void submit(const std::string& body,bool person=false) {
  g_server.body=body; code=0; replyOk=false;
  if(person) handleEmployeeDelete(); else handleSessionDelete();
}
static void rejected(const std::string& body,int expected,bool person=false) {
  const auto before=storemock::files;
  const uint32_t sequence=g_core.lastSequence(),revision=storage::journalRevision();
  submit(body,person);
  CHECK(code==expected); CHECK(!replyOk); CHECK(storemock::files==before);
  CHECK(sequence==g_core.lastSequence() && revision==storage::journalRevision());
}
static std::string alter(const char* field,long long value,bool remove=false) {
  StaticJsonDocument<512> doc; deserializeJson(doc,request(false));
  if(remove) doc.remove(field); else doc[field]=value;
  std::string body; serializeJson(doc,body); return body;
}
int main() {
  seed();
  auth=false; rejected(request(false),0); auth=true;
  csrf=false; rejected(request(false),0); csrf=true;
  bodyAllowed=false; rejected(request(false),0); bodyAllowed=true;
  rejected("{}",400); rejected("not json",400);
  std::string nul=request(false);
  const std::string confirm="OTURUMU KALICI SIL";
  nul.replace(nul.find(confirm),confirm.size(),R"(OTURUMU KALICI SIL\u0000extra)");
  rejected(nul,400);
  rejected(alter("expected_sequence",0,true),400);
  rejected(alter("journal_revision",0,true),409);
  rejected(alter("journal_revision",1),409);
  rejected(alter("journal_revision",-1),409);
  rejected(alter("expected_sequence",3),409);
  rejected(alter("roster_version",g_core.rosterVersion()+1ULL),409);
  rejected(request(false,999),409);
  rejected(request(false,4),409); // R is a protected clock-state record
  rejected(request(false,3),409); // I cancelled by R, not a completed pair
  Event open; CHECK(g_core.preview("AABBCCDD",1800000400UL,open)==Result::Ok);
  CHECK(storage::appendEvent(open)==storage::Status::Ok); CHECK(g_core.commit(open)==Result::Ok);
  rejected(request(true),409,true);
  seed();
  submit(request(false,1)); CHECK(code==200 && replyOk);
  CHECK(storage::journalRevision()==1 && g_core.lastSequence()==2);
  CHECK(g_core.find("11223344")->totalSeconds==0);
  submit(request(true),true); CHECK(code==200 && replyOk);
  CHECK(storage::journalRevision()==2 && g_core.count()==1 && !g_core.find("AABBCCDD"));
  CHECK(storemock::files[storage::g_journalPath].find("41797365")!=std::string::npos);
  CHECK(storemock::files["/wifi.json"]=="wifi-sentinel");
  CHECK(storemock::files["/index.html"]=="html-sentinel");
  CHECK(storemock::formats==0);
  std::printf("delete API guards: %d checks, %d failure(s)\n",checks,failures);
  return failures?1:0;
}
'''
    out = ROOT/'.pio/delete-api-tests'
    out.mkdir(parents=True, exist_ok=True)
    cpp, exe = out/'delete_api.cpp', out/'delete_api.exe'
    cpp.write_text(harness, encoding='utf-8')
    compiler = os.environ.get('CXX') or shutil.which('g++') or str(ROOT/'.platformio/packages/toolchain-gccmingw32/bin/g++.exe')
    env = dict(os.environ, PATH=str(Path(compiler).resolve().parent)+os.pathsep+os.environ.get('PATH', ''))
    subprocess.run([compiler, '-std=c++11', '-Wall', '-Wextra', '-Werror', '-pedantic',
        '-I', str(ROOT/'test/storage_mocks'), '-I', str(ROOT/'include'), '-I', str(ROOT),
        '-I', str(ROOT/'.pio/libdeps/nodemcuv2_sd_soft_tx/ArduinoJson/src'), str(cpp),
        str(ROOT/'src/Attendance.cpp'), '-o', str(exe)], cwd=ROOT, env=env, check=True)
    subprocess.run([str(exe)], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
