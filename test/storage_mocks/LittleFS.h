#pragma once
#include <map>
#include <string>
#include <string.h>
#include <stdint.h>
#include <stddef.h>
namespace storemock {
extern std::map<std::string,std::string> files;
extern bool failWrite,failRead,failRename;
extern int formats;
#ifdef PDKS_REWRITE_FAULTS
void operation();
#endif
}
#ifdef PDKS_REWRITE_FAULTS
#define STORAGE_OPERATION() storemock::operation()
#else
#define STORAGE_OPERATION() ((void)0)
#endif
enum { SeekSet = 0 };
struct FSInfo { size_t totalBytes=1048576,usedBytes=0; };
struct LittleFSConfig { void setAutoFormat(bool) {} };
class File {
 public:
  File() : ok_(false),pos_(0) {}
  File(const char* path,const char* mode) : path_(path),ok_(true),pos_(0) {
    STORAGE_OPERATION();
    if (*mode=='w') storemock::files[path_].clear();
    else if (*mode=='a') { pos_=storemock::files[path_].size(); }
    else if (!storemock::files.count(path_)) ok_=false;
  }
  explicit operator bool() const { return ok_; }
  size_t size() const { return ok_ ? storemock::files[path_].size() : 0; }
  size_t position() const { return pos_; }
  int read() {
    if (!ok_ || storemock::failRead || pos_>=size()) return -1;
    return static_cast<unsigned char>(storemock::files[path_][pos_++]);
  }
  size_t readBytes(char* out,size_t len) {
    size_t n=0; int c=0;
    while(n<len && (c=read())>=0) out[n++]=static_cast<char>(c);
    return n;
  }
  int read(uint8_t* out,size_t len) { return static_cast<int>(readBytes(reinterpret_cast<char*>(out),len)); }
  size_t write(const uint8_t* data,size_t len) {
    STORAGE_OPERATION();
    if(!ok_) return 0;
    if(storemock::failWrite && len) --len;
    storemock::files[path_].replace(pos_,len,reinterpret_cast<const char*>(data),len);
    pos_+=len; return len;
  }
  bool seek(uint32_t pos,int) { if(!ok_ || pos>size()) return false; pos_=pos; return true; }
  void flush() { STORAGE_OPERATION(); }
  void close() { STORAGE_OPERATION(); ok_=false; }
 private:
  std::string path_;
  bool ok_;
  size_t pos_;
};
struct MockFS {
  bool setConfig(const LittleFSConfig&) { return true; }
  bool begin() { return true; }
  bool format() { ++storemock::formats; storemock::files.clear(); return true; }
  bool exists(const char* path) { return storemock::files.count(path)!=0; }
  File open(const char* path,const char* mode) { return File(path,mode); }
  bool remove(const char* path) { STORAGE_OPERATION(); return storemock::files.erase(path)!=0; }
  bool info(FSInfo& info) { info.usedBytes=0; for(const auto& f:storemock::files) info.usedBytes+=f.second.size(); return true; }
  bool rename(const char* from,const char* to) {
    STORAGE_OPERATION();
    if(storemock::failRename || !exists(from)) return false;
    storemock::files[to]=storemock::files[from]; storemock::files.erase(from); return true;
  }
};
extern MockFS LittleFS;
