"""Native guards for the signed-OTA policy header (no hardware, no crypto)."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'.pio/ota-policy-tests'

SOURCE = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#include "OtaPolicy.h"

using namespace pdks::ota_policy;

static int failures = 0;

static void check(bool ok, const char* name) {
  if (!ok) {
    std::printf("FAIL %s\n", name);
    ++failures;
  }
}

static const char* const GOOD_SHA =
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
static const char* const TARGET = "nodemcuv2_sd_soft_tx";

static void testVersionSequence() {
  uint32_t seq = 0xFFFFFFFFu;
  check(versionSequence("1.5.0", seq) && seq == 10500, "version 1.5.0 -> 10500");
  check(versionSequence("0.0.0", seq) && seq == 0, "version 0.0.0 -> 0");
  check(versionSequence("99.99.99", seq) && seq == 999999, "version 99.99.99");
  check(versionSequence("1.5.10", seq) && seq == 10510, "version 1.5.10");
  check(versionSequence("0.1.0", seq) && seq == 100, "version 0.1.0");
  const char* bad[] = {"", "1", "1.5", "1.5.0.1", "1.5.", ".5.0", "01.5.0",
                       "1.05.0", "1.5.00", "1..0", "1.5.-1", "1.5.0 ", " 1.5.0",
                       "1.5.0x", "1.5,0", "a.5.0", "1.100.0", "100.0.0",
                       "1.5.0\n", "+1.5.0", "1.5.0.0", "1.5e0", "1.5.0\r"};
  for (const char* text : bad) {
    seq = 10500;
    check(!versionSequence(text, seq), text);
  }
  check(!versionSequence(nullptr, seq), "version null");
}

static void testLowerHex() {
  check(lowerHex(GOOD_SHA, 64), "lowerHex 64");
  check(!lowerHex(GOOD_SHA, 0), "lowerHex zero length");
  check(!lowerHex(nullptr, 64), "lowerHex null");
  char copy[65];
  std::memcpy(copy, GOOD_SHA, sizeof copy);
  copy[0] = 'A';
  check(!lowerHex(copy, 64), "lowerHex uppercase");
  copy[0] = 'g';
  check(!lowerHex(copy, 64), "lowerHex non-hex");
  copy[0] = ' ';
  check(!lowerHex(copy, 64), "lowerHex space");
}

static void encodeSignature(const uint8_t in[SIGNATURE_BYTES],
                            char out[SIGNATURE_B64_LEN + 1]) {
  static const char alphabet[] =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  size_t used = 0;
  for (size_t i = 0; i < SIGNATURE_BYTES; i += 3) {
    const size_t remain = SIGNATURE_BYTES - i;
    const uint32_t a = in[i];
    const uint32_t b = (remain > 1) ? in[i + 1] : 0u;
    const uint32_t c = (remain > 2) ? in[i + 2] : 0u;
    const uint32_t block = (a << 16) | (b << 8) | c;
    out[used++] = alphabet[(block >> 18) & 63u];
    out[used++] = alphabet[(block >> 12) & 63u];
    out[used++] = (remain > 2) ? alphabet[(block >> 6) & 63u] : '=';
    out[used++] = (remain > 2) ? alphabet[block & 63u] : '=';
  }
  out[used] = '\0';
}

static void testDecodeSignature() {
  const std::string zero = std::string(342, 'A') + "==";
  check(zero.size() == SIGNATURE_B64_LEN, "zero signature length");

  uint8_t sig[SIGNATURE_BYTES];
  std::memset(sig, 0xAA, sizeof sig);
  check(decodeSignature(zero.c_str(), sig), "decode zero signature");
  bool allZero = true;
  for (uint8_t byte : sig) allZero = allZero && (byte == 0);
  check(allZero, "zero signature decodes to zeros");

  uint8_t pattern[SIGNATURE_BYTES];
  for (size_t i = 0; i < SIGNATURE_BYTES; ++i) pattern[i] = (uint8_t)(i * 7u + 3u);
  char encoded[SIGNATURE_B64_LEN + 1];
  encodeSignature(pattern, encoded);
  check(std::strlen(encoded) == SIGNATURE_B64_LEN, "encoded length 344");
  check(std::string(encoded).find("==") == SIGNATURE_B64_LEN - 2, "encoded ends with pad");
  uint8_t roundTrip[SIGNATURE_BYTES];
  std::memset(roundTrip, 0, sizeof roundTrip);
  check(decodeSignature(encoded, roundTrip), "decode nonzero signature");
  check(std::memcmp(pattern, roundTrip, SIGNATURE_BYTES) == 0, "signature round trip");

  std::memset(sig, 0xAA, sizeof sig);
  check(!decodeSignature((std::string(342, 'A')).c_str(), sig), "reject missing pad");
  check(!decodeSignature((std::string(342, 'A') + "A=").c_str(), sig), "reject single pad");
  check(!decodeSignature((std::string(343, 'A')).c_str(), sig), "reject length 343");
  check(!decodeSignature((zero + "A").c_str(), sig), "reject length 345");
  check(!decodeSignature((std::string(341, 'A') + "B==").c_str(), sig), "reject pad bits");
  check(!decodeSignature((std::string(342, '-') + "==").c_str(), sig), "reject url-safe alphabet");
  std::string embedded = zero;
  embedded[10] = '\n';
  check(!decodeSignature(embedded.c_str(), sig), "reject embedded newline");
  std::string inner = zero;
  inner[20] = '=';
  check(!decodeSignature(inner.c_str(), sig), "reject inner pad");
  check(!decodeSignature(nullptr, sig), "decode null");
  bool untouched = true;
  for (uint8_t byte : sig) untouched = untouched && (byte == 0xAA);
  check(untouched, "rejected signature leaves output untouched");
}

static void testEnvelope() {
  char buf[ENVELOPE_MAX];
  const size_t len = envelope(buf, sizeof buf, TARGET, "1.5.0", 10500, 300000, GOOD_SHA);
  const std::string expected = std::string("PDKS-FW1\n") + TARGET + "\n" + "1.5.0\n" +
                               "10500\n300000\n" + GOOD_SHA + "\n";
  check(len == expected.size(), "envelope length");
  check(std::string(buf) == expected, "envelope exact text");
  check(buf[len] == '\0', "envelope terminator");
  check(expected.back() == '\n' && expected.find('\r') == std::string::npos, "envelope canonical tail");

  char other[ENVELOPE_MAX];
  const std::string shaAlt = std::string(63, 'a') + "e";
  const size_t lenAlt = envelope(other, sizeof other, TARGET, "1.6.0", 10600, 300001,
                                 shaAlt.c_str());
  check(lenAlt > 0 && std::string(other) != expected, "envelope binds every field");

  check(envelope(other, sizeof other, TARGET, "1.5.0", 10500, 1048576, GOOD_SHA) > 0 &&
            std::string(other) != expected,
        "envelope binds size upper bound");
  check(envelope(other, sizeof other, TARGET, "1.5.0", 10500, 261, GOOD_SHA) > 0 &&
            std::string(other) != expected,
        "envelope binds size lower bound");

  check(envelope(buf, sizeof buf, "nodemcuv2", "1.5.0", 10500, 300000, GOOD_SHA) == 0,
        "reject unknown target");
  check(envelope(buf, sizeof buf, "nodemcuv2_sd_soft", "1.5.0", 10500, 300000, GOOD_SHA) == 0,
        "reject target suffix");
  check(envelope(buf, sizeof buf, TARGET, "1.5.1", 10500, 300000, GOOD_SHA) == 0,
        "reject version/sequence mismatch");
  check(envelope(buf, sizeof buf, TARGET, "1.5.0", 10501, 300000, GOOD_SHA) == 0,
        "reject sequence mismatch");
  check(envelope(buf, sizeof buf, TARGET, "01.5.0", 10500, 300000, GOOD_SHA) == 0,
        "reject non-canonical version");
  check(envelope(buf, sizeof buf, TARGET, "1.5.0", 10500, 260, GOOD_SHA) == 0,
        "reject size below floor");
  check(envelope(buf, sizeof buf, TARGET, "1.5.0", 10500, 1048577, GOOD_SHA) == 0,
        "reject size above ceiling");

  const std::string upper = "0123456789ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef";
  check(envelope(buf, sizeof buf, TARGET, "1.5.0", 10500, 300000, upper.c_str()) == 0,
        "reject uppercase sha");
  check(envelope(buf, sizeof buf, TARGET, "1.5.0", 10500, 300000, std::string(63, 'a').c_str()) == 0,
        "reject short sha");
  check(envelope(buf, sizeof buf, TARGET, "1.5.0", 10500, 300000, std::string(65, 'a').c_str()) == 0,
        "reject long sha");
  check(envelope(buf, sizeof buf, TARGET, "1.5.0", 10500, 300000, nullptr) == 0,
        "reject null sha");
  check(envelope(buf, sizeof buf, nullptr, "1.5.0", 10500, 300000, GOOD_SHA) == 0,
        "reject null target");
  check(envelope(buf, expected.size(), TARGET, "1.5.0", 10500, 300000, GOOD_SHA) == 0,
        "reject short capacity");
  check(envelope(buf, expected.size() + 1, TARGET, "1.5.0", 10500, 300000, GOOD_SHA) ==
            expected.size(),
        "accept exact capacity");
  check(envelope(nullptr, 0, TARGET, "1.5.0", 10500, 300000, GOOD_SHA) == 0,
        "reject null output");
}

static void testMayPrepare() {
  check(mayPrepare(true, true, true, false, 10500, 10501), "prepare ok");
  check(!mayPrepare(false, true, true, false, 10500, 10501), "prepare needs auth");
  check(!mayPrepare(true, false, true, false, 10500, 10501), "prepare needs csrf");
  check(!mayPrepare(true, true, false, false, 10500, 10501), "prepare needs healthy storage");
  check(!mayPrepare(true, true, true, true, 10500, 10501), "prepare refuses anyone inside");
  check(!mayPrepare(true, true, true, false, 10500, 10500), "prepare refuses same sequence");
  check(!mayPrepare(true, true, true, false, 10500, 10499), "prepare refuses older sequence");
  check(!mayPrepare(true, true, true, false, 10500, 0), "prepare refuses zero sequence");
  check(mayPrepare(true, true, true, false, 0, 1), "prepare accepts first sequence");
}

int main() {
  testVersionSequence();
  testLowerHex();
  testDecodeSignature();
  testEnvelope();
  testMayPrepare();
  if (failures != 0) {
    std::printf("ota policy: %d failure(s)\n", failures);
    return 1;
  }
  std::printf("ota policy: all checks passed\n");
  return 0;
}
'''


def compiler():
    return os.environ.get('CXX') or shutil.which('g++') or str(
        ROOT/'.platformio/packages/toolchain-gccmingw32/bin/g++.exe')


def standard(tool, env):
    """Newest supported standard; the local pio mingw g++ 5.1 only knows C++11."""
    try:
        version = subprocess.run([tool, '-dumpversion'], capture_output=True, text=True,
                                 env=env, check=True).stdout.strip()
        major = int(version.split('.')[0])
    except (OSError, ValueError, subprocess.CalledProcessError):
        major = 5
    return '-std=c++20' if major >= 10 else '-std=c++11'


def main():
    tool = compiler()
    OUT.mkdir(parents=True, exist_ok=True)
    source = OUT/'ota_policy_main.cpp'
    source.write_text(SOURCE, encoding='utf-8')
    env = dict(os.environ, PATH=str(Path(tool).resolve().parent)+os.pathsep+os.environ.get('PATH', ''))
    exe = OUT/'ota_policy.exe'
    subprocess.run([tool, standard(tool, env), '-Wall', '-Wextra', '-Werror', '-pedantic',
                    '-I', str(ROOT/'include'), str(source), '-o', str(exe)],
                   cwd=ROOT, env=env, check=True)
    subprocess.run([str(exe)], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
