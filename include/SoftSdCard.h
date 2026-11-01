#pragma once

#include <stddef.h>

namespace pdks {
namespace softsd {

// Mounts the optional SD mirror on the software-SPI bus (FAT16/FAT32 only).
// Never formats, truncates or renames anything.
bool begin();

// Appends one raw journal line (without a trailing newline) to /events.log,
// then adds '\n', syncs, closes and verifies the appended bytes by reading
// them back. Returns false when the mirror is not initialised or any step
// fails. Does not create a new file unless /events.log is absent.
bool appendLine(const char* data, size_t length);

// Explicit authorised rewrite/maintenance only: removes ONLY /events.log.
bool resetEvents();

} // namespace softsd
} // namespace pdks
