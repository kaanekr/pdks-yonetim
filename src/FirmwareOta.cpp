#include "FirmwareOta.h"
#include "ReleaseVersion.h"

#if PDKS_ENABLE_OTA
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>
#include <BearSSLHelpers.h>
#include <StackThunk.h>
#include <Updater.h>
#include <string.h>
#include "Attendance.h"
#include "Config.h"
#include "DeviceAuth.h"
#include "OtaPolicy.h"
#include "Storage.h"
#include "UpdatePublicKey.h"

static_assert(cfg::SD_SOFTSPI && cfg::PIN_BUZZER == 1 && !cfg::UART_LOGGING,
              "OTA target must match the deployed TX buzzer / D0 SD input");
namespace pdks { namespace ota { namespace {
ESP8266WebServer* web = nullptr;
Attendance* core = nullptr;
const char* csrf = nullptr;
const bool* healthy = nullptr;
BearSSL::PublicKey publicKey(PDKS_UPDATE_PUBLIC_KEY);
BearSSL::HashSHA256 imageHash, envelopeHash, transferHash;
BearSSL::SigningVerifier verifier(&publicKey);
struct Prepared {
  uint32_t size=0, offered=0, sequence=0, roster=0, revision=0, at=0;
  char sha[65]={}, token[33]={};
  bool ready=false;
} prepared;
// The ESP8266 loop stack is small; keep the large fixed buffers out of the
// web handler frame. The server is single-threaded, so reuse is safe.
char envelope[ota_policy::ENVELOPE_MAX+1];
uint8_t decoded[256];
bool active=false, ended=false, seen=false, reboot=false;
uint32_t received=0, restartAt=0;
uint8_t tail[4]={};
size_t tailCount=0;
const char* error=nullptr;

void reply(int code, bool ok, const char* message) {
  String text = ok ? "{\"ok\":true,\"message\":\"" : "{\"ok\":false,\"message\":\"";
  text += message; text += "\"}";
  web->sendHeader("Cache-Control", "no-store");
  web->send(code,"application/json",text);
}
bool secure() {
  return deviceauth::ready() && web->authenticate(deviceauth::username(),deviceauth::password()) &&
      csrf && strlen(csrf)==32 && web->header("X-CSRF-Token").length()==32 &&
      strcmp(web->header("X-CSRF-Token").c_str(),csrf)==0;
}
bool unchanged(bool expiry) {
  return prepared.ready && (!expiry || millis()-prepared.at<=60000UL) &&
      healthy && *healthy && storage::mounted() && !storage::locked() && !core->anyInside() &&
      core->lastSequence()==prepared.sequence && core->rosterVersion()==prepared.roster &&
      storage::journalRevision()==prepared.revision;
}
void fail(const char* message) {
  error=message;
  // The last four bytes are deliberately withheld until the final handler.
  // end(false) therefore cannot accidentally commit a fully received but rejected upload.
  if (Update.isRunning()) Update.end(false);
  active=false; ended=false; prepared.ready=false; tailCount=0;
}
bool textField(JsonDocument& doc,const char* name,const char*& value) {
  value=doc[name] | "";
  return doc[name].is<const char*>() && doc[name].as<JsonString>().size()==strlen(value);
}
void prepare() {
  if (active || reboot) { reply(409,false,"GUNCELLEME SURUYOR"); return; }
  if (!secure()) { reply(403,false,"KIMLIK/CSRF REDDEDILDI"); return; }
  if (web->arg("plain").length()>1024) { reply(413,false,"ISTEK COK BUYUK"); return; }
  prepared=Prepared(); error=nullptr; seen=false; ended=false; received=0; tailCount=0;
  DynamicJsonDocument doc(1536);
  if (doc.capacity()<1536) { reply(500,false,"BELLEK YETERSIZ"); return; }
  if (deserializeJson(doc,web->arg("plain"))) { reply(400,false,"JSON GECERSIZ"); return; }
  const char *target,*version,*digest,*signature,*confirm;
  if (!textField(doc,"target",target) || !textField(doc,"version",version) ||
      !textField(doc,"sha256",digest) || !textField(doc,"meta_signature",signature) ||
      !textField(doc,"confirm",confirm) || strcmp(confirm,"FIRMWARE GUNCELLE") ||
      !doc["sequence"].is<uint32_t>() || !doc["size"].is<uint32_t>() ||
      !doc["expected_sequence"].is<uint32_t>() || !doc["roster_version"].is<uint32_t>() ||
      !doc["journal_revision"].is<uint32_t>()) { reply(400,false,"ALAN/ONAY GECERSIZ"); return; }
  const uint32_t offered=doc["sequence"].as<uint32_t>(), size=doc["size"].as<uint32_t>();
  if (!ota_policy::mayPrepare(true,true,healthy && *healthy && storage::mounted() && !storage::locked(),core->anyInside(),release::SEQUENCE,offered) ||
      doc["expected_sequence"].as<uint32_t>()!=core->lastSequence() ||
      doc["roster_version"].as<uint32_t>()!=core->rosterVersion() ||
      doc["journal_revision"].as<uint32_t>()!=storage::journalRevision()) {
    reply(409,false,"DURUM DEGISTI/ACIK OTURUM/ESKI SURUM; YENILEYIN"); return;
  }
  const size_t length=ota_policy::envelope(envelope,sizeof(envelope),target,version,offered,size,digest);
  if (!length || !ota_policy::decodeSignature(signature,decoded)) { reply(400,false,"IMZALI META GECERSIZ"); return; }
  envelopeHash.begin(); envelopeHash.add(envelope,length); envelopeHash.end();
  if (!verifier.verify(&envelopeHash,decoded,sizeof(decoded))) { reply(403,false,"YAYINCI IMZASI GECERSIZ"); return; }
  const uint32_t free=ESP.getFreeSketchSpace();
  if (free<=4096 || size>((free-4096)&0xFFFFF000u)) { reply(409,false,"OTA ICIN FLASH ALANI YETERSIZ"); return; }
  if (storage::replayJournal(nullptr,nullptr)!=storage::Status::Ok) { reply(409,false,"JOURNAL DOGRULANAMADI"); return; }
  prepared.size=size; prepared.offered=offered; prepared.sequence=core->lastSequence();
  prepared.roster=core->rosterVersion(); prepared.revision=storage::journalRevision();
  memcpy(prepared.sha,digest,65);
  for(unsigned i=0;i<4;++i) snprintf(prepared.token+i*8,9,"%08lX",(unsigned long)ESP.random());
  prepared.at=millis(); prepared.ready=true;
  String response="{\"ok\":true,\"token\":\""; response+=prepared.token; response+="\"}";
  web->sendHeader("Cache-Control","no-store"); web->send(200,"application/json",response);
}
bool writeBytes(const uint8_t* data,size_t size) {
  return !size || Update.write(const_cast<uint8_t*>(data),size)==size;
}
bool writeDeferred(const uint8_t* data,size_t size) {
  if (size>=4) {
    if (!writeBytes(tail,tailCount) || !writeBytes(data,size-4)) return false;
    memcpy(tail,data+size-4,4); tailCount=4;
  } else {
    uint8_t joined[8]; memcpy(joined,tail,tailCount); memcpy(joined+tailCount,data,size);
    const size_t total=tailCount+size, flush=total>4 ? total-4 : 0;
    if (!writeBytes(joined,flush)) return false;
    tailCount=total-flush; memcpy(tail,joined+flush,tailCount);
  }
  return true;
}
void upload() {
  HTTPUpload& file=web->upload();
  if (file.status==UPLOAD_FILE_START) {
    if (seen || error || reboot || file.name!="firmware" || !secure() || !unchanged(true) ||
        web->header("X-OTA-Token").length()!=32 || strcmp(web->header("X-OTA-Token").c_str(),prepared.token)) {
      fail("YUKLEME ONAYI/DURUMU GECERSIZ"); return;
    }
    seen=true; received=0; tailCount=0; ended=false; transferHash.begin();
    if (!Update.begin(prepared.size,U_FLASH,-1)) { fail("FLASH HAZIRLANAMADI"); return; }
    active=true;
  } else if (file.status==UPLOAD_FILE_WRITE && active && !error) {
    const size_t size=file.currentSize;
    if (size>prepared.size-received) { fail("FAZLA FIRMWARE VERISI"); return; }
    transferHash.add(file.buf,size);
    if (!writeDeferred(file.buf,size)) { fail("FLASH YAZMA BASARISIZ"); return; }
    received+=size;
  } else if (file.status==UPLOAD_FILE_END && active && !error) ended=true;
  else if (file.status==UPLOAD_FILE_ABORTED) fail("YUKLEME KESILDI; TEKRAR OTOMATIK GONDERILMEDI");
}
void finish() {
  if (error) { reply(400,false,error); return; }
  if (!secure() || !active || !ended || !unchanged(false) || received!=prepared.size || tailCount!=4) {
    fail("YUKLEME TAMAMLANMADI/ONAY GECERSIZ"); reply(409,false,error); return;
  }
  transferHash.end();
  const uint8_t* hash=static_cast<const uint8_t*>(transferHash.hash());
  char digest[65];
  for(unsigned i=0;i<32;++i) snprintf(digest+i*2,3,"%02x",hash[i]);
  if (strcmp(digest,prepared.sha) || tail[0]!=0 || tail[1]!=1 || tail[2]!=0 || tail[3]!=0) {
    fail("FIRMWARE OZETI/IMZA BOYU UYUSMUYOR"); reply(400,false,error); return;
  }
  // U_FLASH only; never touch LittleFS. Native BearSSL verifies the signed image.
  if (!writeBytes(tail,4) || !Update.end(false)) {
    fail("FIRMWARE IMZASI/FLASH DOGRULANAMADI"); reply(400,false,error); return;
  }
  active=false; prepared.ready=false; reboot=true; restartAt=millis()+1500;
  reply(200,true,"IMZALI FIRMWARE DOGRULANDI; CIHAZ YENIDEN BASLAYACAK");
}
} // namespace
void setup(ESP8266WebServer& server,Attendance& attendance,const char* token,const bool* storageHealthy) {
  web=&server; core=&attendance; csrf=token; healthy=storageHealthy;
  // Manual installSignature does not initialise the BearSSL second stack.
  // SigningVerifier::verify thunks onto it; without this the device resets.
  stack_thunk_add_ref();
  Update.installSignature(&imageHash,&verifier);
  web->on("/api/update/prepare",HTTP_POST,prepare);
  web->on("/api/update/firmware",HTTP_POST,finish,upload);
}
bool busy() { return active || reboot; }
void tick() {
  if (reboot && static_cast<int32_t>(millis()-restartAt)>=0) ESP.restart();
  if (!active && prepared.ready && millis()-prepared.at>60000UL) prepared.ready=false;
}
} }
#else
namespace pdks { namespace ota {
void setup(ESP8266WebServer&,Attendance&,const char*,const bool*) {}
bool busy() { return false; }
void tick() {}
} }
#endif
