/* Lines that came once per frame or per resource become counts: each is still counted, only some are printed. */
#pragma once
#include <atomic>
#include <cstdint>
namespace fuxi {
// The first eight, then powers of two: bounded log volume, cumulative count (the sampling refusals' rule).
inline bool logSampled(uint64_t count){return count<=8 || !(count&(count-1));}
// IOSurface imports that passed (cache hits included); new render target kinds. The cache period line prints them.
inline std::atomic<uint64_t> surfacePassLogs{0},renderTargetLogs{0};
}
