#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Fixed 128x32 layout, using the GFX classic 6x8 character cell. No GPIO,
// allocation, delays or storage access; the same renderer is host-tested.
namespace pdks {
namespace display {

enum class View : uint8_t { Clock, Entry, Exit, Notice };
enum class Align : uint8_t { Left, Center, Right };

struct Screen {
  View view = View::Clock;
  const char* time = "--:--:--";
  const char* date = "--.--";
  const char* year = "----";
  const char* title = "";
  const char* detail = "";
  const char* hint = "";
  bool sdEnabled = false;
  bool sdOk = false;
};

// ASCII text only (main converts Turkish UTF-8 before constructing messages).
// Clip explicitly instead of allowing GFX to wrap or silently cut glyphs.
template <class Gfx>
void text(Gfx& gfx, int16_t x, int16_t y, uint8_t width, const char* value,
          uint8_t size = 1, Align align = Align::Left, bool inverse = false) {
  if (!value || !size) return;
  size_t limit = width / (6 * size);
  if (limit > 21) limit = 21;
  char line[22];
  size_t count = 0;
  while (count < limit && value[count]) {
    line[count] = value[count];
    ++count;
  }
  if (count == limit && value[count] && count >= 3) {
    line[count - 3] = '.';
    line[count - 2] = '.';
    line[count - 1] = '.';
  }
  line[count] = '\0';
  const int16_t remaining = width - static_cast<int16_t>(count * 6 * size);
  if (align == Align::Center) x += remaining / 2;
  else if (align == Align::Right) x += remaining;
  gfx.setTextSize(size);
  gfx.setTextColor(inverse ? 0 : 1);
  gfx.setCursor(x, y);
  gfx.print(line);
}

// Compact 4x6 capitals at 2x: 8x12 ink instead of the classic 10x14.
// The 64x16 background stays unchanged; five letters occupy 48x12, centred.
template <class Gfx>
void badgeLabel(Gfx& gfx, bool entry) {
  static const char letters[] = "CGIKRS";
  static const uint8_t glyphs[][6] = {
    {0x7, 0x8, 0x8, 0x8, 0x8, 0x7}, // C
    {0x6, 0x8, 0xB, 0x9, 0x9, 0x6}, // G
    {0xE, 0x4, 0x4, 0x4, 0x4, 0xE}, // I
    {0x9, 0xA, 0xC, 0xC, 0xA, 0x9}, // K
    {0xE, 0x9, 0xE, 0xA, 0x9, 0x9}, // R
    {0x7, 0x8, 0x6, 0x1, 0x9, 0x6}, // S
  };
  const char* label = entry ? "GIRIS" : "CIKIS";
  for (uint8_t i = 0; i < 5; ++i) {
    const uint8_t* glyph = glyphs[strchr(letters, label[i]) - letters];
    for (uint8_t row = 0; row < 6; ++row) {
      for (uint8_t col = 0; col < 4; ++col) {
        if (glyph[row] & (0x8 >> col))
          gfx.fillRect(8 + i * 10 + col * 2, 18 + row * 2, 2, 2, 0);
      }
    }
  }
}

template <class Gfx>
void render(Gfx& gfx, const Screen& screen) {
  gfx.clearDisplay();
  gfx.setFont(nullptr);
  gfx.setTextWrap(false);
  if (screen.view == View::Clock) {
    char hoursMinutes[6] = {};
    const char* time = screen.time ? screen.time : "--:--:--";
    strncpy(hoursMinutes, time, 5);
    const bool sdError = screen.sdEnabled && !screen.sdOk;
    if (sdError) {
      // Reserve the lower row for a real fault, never cover the large digits.
      text(gfx, 0, 1, 60, hoursMinutes, 2);
      gfx.drawFastVLine(88, 0, 17, 1);
      text(gfx, 94, 0, 34, screen.date, 1, Align::Right);
      text(gfx, 94, 9, 34, screen.year, 1, Align::Right);
      gfx.fillRect(84, 23, 44, 9, 1);
      text(gfx, 84, 24, 44, "SD HATA", 1, Align::Right, true);
    } else {
      // Use the full panel height, without reintroducing an idle status row.
      text(gfx, 0, 4, 90, hoursMinutes, 3);
      gfx.drawFastVLine(94, 0, 32, 1);
      text(gfx, 98, 6, 30, screen.date, 1, Align::Right);
      text(gfx, 98, 18, 30, screen.year, 1, Align::Right);
    }
  } else if (screen.view == View::Entry || screen.view == View::Exit) {
    const char* name = screen.title ? screen.title : "";
    const uint8_t nameSize = strlen(name) <= 10 ? 2 : 1;
    text(gfx, 0, nameSize == 2 ? 0 : 4, 128, name, nameSize, Align::Center);
    gfx.fillRect(0, 16, 64, 16, 1);
    badgeLabel(gfx, screen.view == View::Entry);
    const char* detail = screen.detail ? screen.detail : "";
    const char* label = screen.view == View::Entry ? "SAAT" : "SURE";
    char compact[11] = {};
    // Very long durations retain all hours/minutes instead of clipping.
    // Original full duration remains unchanged in the journal and panel.
    if (screen.view == View::Exit && strlen(detail) > 10) {
      const char* lastColon = strrchr(detail, ':');
      if (lastColon && static_cast<size_t>(lastColon - detail) <= 10) {
        memcpy(compact, detail, static_cast<size_t>(lastColon - detail));
        detail = compact;
        label = "SURE H:DK";
      }
    }
    text(gfx, 68, 16, 60, label);
    text(gfx, 68, 24, 60, detail);
  } else {
    gfx.fillRect(0, 0, 128, 10, 1);
    text(gfx, 0, 1, 128, screen.title, 1, Align::Center, true);
    const bool hasHint = screen.hint && screen.hint[0];
    const char* detail = screen.detail ? screen.detail : "";
    const uint8_t size = !hasHint && strlen(detail) <= 10 ? 2 : 1;
    text(gfx, 0, hasHint ? 13 : (size == 2 ? 14 : 18), 128, detail, size, Align::Center);
    if (hasHint) text(gfx, 0, 24, 128, screen.hint, 1, Align::Center);
  }
  // Leave predictable defaults for the startup/reinitialisation path.
  gfx.setTextSize(1);
  gfx.setTextColor(1);
  gfx.display();
}

} // namespace display
} // namespace pdks
