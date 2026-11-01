#pragma once

// Explicitly authorised maintenance only, never called by the normal firmware.
// A durable pending marker precedes deletion; atomic pending->done commits it.
// Reboot with the same completed request NEVER deletes later-added employees.
namespace pdks {
namespace reset {

constexpr const char* PENDING = "/attendance-reset.pending";
constexpr const char* DONE = "/attendance-reset.done";
constexpr const char* TEMP = "/attendance-reset.tmp";
enum class Marker { Missing, Match, Other, Invalid };
enum class Result { Cleared, AlreadyDone, Failed };

template <class Io>
Result run(Io& io, const char* request) {
  if (!request || !*request) return Result::Failed;
  unsigned length = 0;
  for (; request[length]; ++length)
    if (length >= 10 || request[length] < '0' || request[length] > '9')
      return Result::Failed;
  const Marker pending = io.marker(PENDING, request);
  const Marker done = io.marker(DONE, request);
  if (pending == Marker::Invalid || pending == Marker::Other ||
      done == Marker::Invalid) return Result::Failed;
  if (done == Marker::Match)
    return pending == Marker::Missing ? Result::AlreadyDone : Result::Failed;
  if (pending == Marker::Missing && !io.prepare(request)) return Result::Failed;
  if (io.marker(PENDING, request) != Marker::Match) return Result::Failed;

  // SD must be available: do not silently leave an old personnel-event mirror.
  if (!io.clearSdEvents()) return Result::Failed;
  const char* paths[] = {"/journal.log", "/employees.tmp", "/employees.json"};
  for (const char* path : paths) {
    if (!io.removeFile(path)) return Result::Failed;
  }
  if (!io.commit()) return Result::Failed;
  return io.marker(DONE, request) == Marker::Match &&
         io.marker(PENDING, request) == Marker::Missing
             ? Result::Cleared : Result::Failed;
}

} // namespace reset
} // namespace pdks
