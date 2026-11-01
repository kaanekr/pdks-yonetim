#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <string>
#include <vector>

#include "DisplayUi.h"

static int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; \
  printf("FAIL line %d: %s\n", __LINE__, #x); } } while (0)

struct Text {
  int x, y, size, color;
  std::string value;
};

struct Rect { int x, y, w, h, color; };

// Validate complete classic-font cells, not just GFX's built-in clipping.
struct Canvas {
  std::vector<Text> texts;
  std::vector<Rect> rects;
  int x = 0, y = 0, size = 1, color = 1, flushes = 0;
  void clearDisplay() { texts.clear(); rects.clear(); }
  void setFont(const void* f) { CHECK(f == nullptr); }
  void setTextWrap(bool wrap) { CHECK(!wrap); }
  void setTextSize(uint8_t value) { size = value; }
  void setTextColor(uint16_t value) { color = value; }
  void setCursor(int16_t px, int16_t py) { x = px; y = py; }
  void print(const char* value) {
    CHECK(x >= 0); CHECK(y >= 0);
    CHECK(x + static_cast<int>(strlen(value)) * 6 * size <= 128);
    CHECK(y + 8 * size <= 32);
    CHECK(strchr(value, '\n') == nullptr);
    texts.push_back(Text{x, y, size, color, value});
  }
  void fillRect(int x0, int y0, int w, int h, int c) {
    CHECK(x0 >= 0 && y0 >= 0 && x0 + w <= 128 && y0 + h <= 32);
    CHECK(c == 0 || c == 1);
    rects.push_back(Rect{x0, y0, w, h, c});
  }
  void drawFastHLine(int x0, int y0, int w, int c) { fillRect(x0, y0, w, 1, c); }
  void drawFastVLine(int x0, int y0, int h, int c) { fillRect(x0, y0, 1, h, c); }
  void display() { ++flushes; CHECK(size == 1); CHECK(color == 1); }
  const Text* find(const char* value) const {
    for (const Text& text : texts) if (text.value == value) return &text;
    return nullptr;
  }
};

static void checkNoTextOverlap(const Canvas& canvas) {
  for (size_t i = 0; i < canvas.texts.size(); ++i) {
    const Text& a = canvas.texts[i];
    for (size_t j = i + 1; j < canvas.texts.size(); ++j) {
      const Text& b = canvas.texts[j];
      CHECK(a.x + static_cast<int>(a.value.size()) * 6 * a.size <= b.x ||
            b.x + static_cast<int>(b.value.size()) * 6 * b.size <= a.x ||
            a.y + 8 * a.size <= b.y || b.y + 8 * b.size <= a.y);
    }
  }
}

static void checkBadge(const Canvas& canvas, bool entry) {
  int backgrounds = 0, blackPixels = 0;
  for (const Rect& rect : canvas.rects) {
    if (rect.color == 1 && rect.x == 0 && rect.y == 16 && rect.w == 64 && rect.h == 16)
      ++backgrounds;
    if (rect.color == 0) {
      ++blackPixels;
      CHECK(rect.w == 2 && rect.h == 2);
      CHECK(rect.x >= 8 && rect.x + rect.w <= 56);
      CHECK(rect.y >= 18 && rect.y + rect.h <= 30);
    }
  }
  CHECK(backgrounds == 1); // same original background, never reduced
  CHECK(blackPixels > 50);
  // G has its extra horizontal stroke; C does not. Check the first letter.
  bool gStroke = false;
  for (const Rect& rect : canvas.rects)
    if (rect.color == 0 && rect.x == 12 && rect.y == 22) gStroke = true;
  CHECK(gStroke == entry);
}

int main() {
  using namespace pdks::display;
  Canvas canvas;
  Screen screen;
  screen.time = "08:05:09"; screen.date = "24.10"; screen.year = "2026";
  screen.sdEnabled = true; screen.sdOk = true;
  render(canvas, screen);
  CHECK(canvas.find("08:05") && canvas.find("08:05")->size == 3);
  CHECK(canvas.find("08:05")->y == 4);
  CHECK(!canvas.find(":09")); CHECK(!canvas.find("08:05:09"));
  CHECK(canvas.find("24.10")); CHECK(canvas.find("2026"));
  CHECK(canvas.find("2026")->y == 18);
  checkNoTextOverlap(canvas);
  CHECK(!canvas.find("KART OKUT")); CHECK(!canvas.find("SD OK"));
  CHECK(!canvas.find("HAZIR")); CHECK(canvas.texts.size() == 3);
  CHECK(canvas.flushes == 1);
  screen.sdOk = false;
  render(canvas, screen);
  CHECK(canvas.find("SD HATA") && canvas.find("SD HATA")->color == 0);
  CHECK(canvas.find("08:05")->size == 2);
  CHECK(!canvas.find(":09")); CHECK(canvas.texts.size() == 4);
  checkNoTextOverlap(canvas);
  screen.sdEnabled = false;
  render(canvas, screen);
  CHECK(!canvas.find("HAZIR")); CHECK(!canvas.find("SD HATA"));
  CHECK(!canvas.find("KART OKUT")); CHECK(canvas.texts.size() == 3);
  CHECK(!canvas.find(":09"));
  CHECK(canvas.find("08:05")->size == 3);
  checkNoTextOverlap(canvas);

  screen.view = View::Entry; screen.title = "Umut Eker"; screen.detail = "08:05:09";
  render(canvas, screen);
  CHECK(canvas.find("Umut Eker") && canvas.find("Umut Eker")->size == 2);
  checkBadge(canvas, true);
  CHECK(canvas.find("SAAT")); CHECK(canvas.find("08:05:09"));
  screen.view = View::Exit; screen.detail = "7:35:12";
  render(canvas, screen);
  checkBadge(canvas, false);
  CHECK(canvas.find("SURE")); CHECK(canvas.find("7:35:12"));
  const char* durations[] = {"00:07", "1:00:01", "99:59:59", "100:00:00", "9999:59:59"};
  for (const char* duration : durations) {
    screen.detail = duration; render(canvas, screen); CHECK(canvas.find(duration));
  }
  screen.detail = "1193046:28:15";
  render(canvas, screen);
  CHECK(canvas.find("1193046:28")); CHECK(canvas.find("SURE H:DK"));

  // Every name length through the roster limit; no wrapping or partial cells.
  for (size_t length = 0; length <= 48; ++length) {
    std::string name(length, 'A'); screen.title = name.c_str();
    render(canvas, screen);
    CHECK(canvas.texts[0].size == (length <= 10 ? 2 : 1));
    CHECK(canvas.texts[0].value.size() <= (length <= 10 ? 10u : 21u));
    if (length > 21) CHECK(canvas.texts[0].value.substr(18) == "...");
  }
  screen.view = View::Notice; screen.title = "SAAT HATALI";
  screen.detail = "SAAT AYARLANMAMIS"; screen.hint = "PANELDEN SAATI AYARLA";
  render(canvas, screen);
  CHECK(canvas.find("SAAT HATALI")->color == 0);
  CHECK(canvas.find("PANELDEN SAATI AYARLA"));
  screen.title = "KART TANIMSIZ"; screen.detail = "UID 01234567890123456789"; screen.hint = "";
  render(canvas, screen);
  CHECK(canvas.texts[1].value.size() == 21);
  CHECK(canvas.texts[1].value.substr(18) == "...");
  screen.detail = "KAYIT RED";
  render(canvas, screen);
  CHECK(canvas.find("KAYIT RED")->size == 2);
  screen.title = nullptr; screen.detail = nullptr; screen.hint = nullptr;
  render(canvas, screen);
  screen.view = View::Entry; render(canvas, screen);
  screen.view = View::Clock; screen.time = nullptr; screen.date = nullptr; screen.year = nullptr;
  render(canvas, screen);
  CHECK(canvas.find("--:--"));
  screen.time = "1"; render(canvas, screen);
  printf("OLED layout: %d checks, %d failure(s)\n", checks, failures);
  return failures ? 1 : 0;
}
