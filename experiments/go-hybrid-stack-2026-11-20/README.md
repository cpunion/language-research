# Hybrid goroutine stacks in Go 1.27.3

[简体中文](README.zh-CN.md)

This experiment runs a hybrid stack allocator in the Go runtime. A goroutine starts with a pooled, copying stack. On a growth request between 64 KiB and 1 MiB, the experiment tries to move it into a 1 MiB virtual range. Subsequent growth and shrinkage within that range keep frame addresses stable. The normal copy path handles the first migration and any overflow beyond 1 MiB, including pointer and channel-waiter adjustment.

## Implementation

- The Go stack guard and doubling policy still determine when and how far to grow. On promotion, the runtime reserves the range with one inaccessible page at its lower end. The rest is anonymous writable memory backed on first touch. In-range growth changes the software bounds without an OS call. On Linux the allocator asks the kernel to disable transparent huge pages for that range.
- The existing GC shrink policy determines when to shrink. The smallest usable VM stack is one OS page. Linux discards the unused prefix with `MADV_DONTNEED`. macOS replaces it with a fresh anonymous mapping at the same address. The kernel supplies zeroed pages on later touches. Exiting goroutines unmap the range. Only ordinary stacks enter the Go stack pool.
- The experiment adds a marker to `g` and uses `StackSys` to account for currently usable VM stack capacity. Process RSS measures physical residency separately. Each promoted goroutine consumes one 1 MiB virtual range plus its guard page, including when it shrinks to one page.

The source overlay is pinned to Go 1.27.3 and leaves the installed toolchain and source tree untouched. `GODEBUG=hybridstack=1` opts in on 64-bit macOS and Linux (`arm64` and `amd64`). The experiment was run on `arm64`. `linux/amd64` and the `windows/amd64` copying fallback were cross-compiled. The overlay uses a pure Go build. Promotion is disabled for race, ASan, MSan, Valgrind, RuntimeSecret, allocation tracing, and stack debugging modes that depend on the copying allocator's memory ownership or diagnostics. The Go compiler's stack checks still enforce the usable bound within the writable virtual range. [Go stack implementation](https://github.com/golang/go/blob/go1.27.3/src/runtime/stack.go) · [Go OS memory abstraction](https://github.com/golang/go/blob/go1.27.3/src/runtime/mem.go)

## Reproduce

On supported macOS or Linux hosts, use Go 1.27.3 and Python 3.9 or later.

```sh
python3 build.py -o /tmp/go-hybrid-stack
python3 run.py /tmp/go-hybrid-stack -o results.json
```

`build.py` can also cross-compile the probe using `--goos linux --goarch arm64`. Run the probe and `run.py` on the target OS to measure it. The runner compares modes of the same binary using `GOMAXPROCS=4` with adaptive starting stack sizes disabled. It runs 31 alternating-order pairs per workload after warmups. Elapsed times are measured within the Go process, and the workloads verify equal checksums. Peak process RSS comes from `wait4` on macOS and `VmHWM` from `/proc/self/status` on Linux.

The correctness probe checks a 2 KiB starting stack, promotion at 64 KiB, in-place growth to 512 KiB, GC shrink to one page, and regrowth to 512 KiB at the same address. It also checks fallback at 2 MiB and cleanup on exit. The recursion retains pointers into parent frames across relocation. A weak reference to an object held by a blocked goroutine checks GC reachability before the channel delivers another pointer. The runner repeats the checks with execution tracing and `gccheckmark=1` enabled.

All 18 selected tests from Go's runtime suite passed with the hybrid enabled on both Arm64 targets. To reproduce on a local target, compile and run them as follows.

```sh
python3 build.py --runtime-tests -o /tmp/go-hybrid-runtime.test
GODEBUG=hybridstack=1,adaptivestackstart=0 GOMAXPROCS=4 \
  /tmp/go-hybrid-runtime.test -test.v -test.timeout=2m \
  -test.run '^(TestStack(Mem|Growth|Cache|Panic|Output|AllOutput)|Test(PanicUseStack|ShrinkStackDuringBlockedSend|NoShrinkStackWhileParking|SelectStackAdjust|ChanSendSelectBarrier|ChanSendBarrier|GCTestMoveStackOnNextCall|GCTestMoveStackRepeatedly|ReadMemStats|MemStats|PreemptionGC|FPUnwindStackGrowthAfterRecovery))$'
```

## Measurements

Raw paired timings, process RSS and correctness results are available for [macOS 27.0.1](darwin-arm64.json) and [Linux 7.0.14 under OrbStack](linux-arm64.json). Both ran on the same Apple M4 Max. The OS page sizes were 16 KiB on macOS and 4 KiB on Linux. Each recursive benchmark frame retains a 4 KiB local array. Garbage collection is disabled for the timed workloads except for explicit collections in the repeated growth workload.

| Workload | macOS copy → hybrid | Linux copy → hybrid |
|---|---:|---:|
| 16,384 shallow goroutines | 7.46 → 7.50 ms | 10.00 → 9.73 ms |
| One goroutine, 201 recursive frames | 0.180 → 0.094 ms | 0.148 → 0.168 ms |
| 512 short-lived goroutines, 81 frames each | 4.89 → 6.39 ms | 8.72 → 18.89 ms |
| Eight growth cycles in one goroutine, with four collections per cycle | 3.65 → 3.35 ms | 45.00 → 46.33 ms |

The table shows per-mode medians. The median paired time ratio for one deep stack was 0.55 on macOS and 1.14 on Linux. The Linux timings overlapped substantially. The shallow workloads stayed on pooled stacks and had the same median peak RSS at the displayed precision, 48.61 MiB on macOS and 46.45 MiB on Linux. For one deep stack, median peak RSS fell from 6.86 to 5.38 MiB on macOS and from 4.74 to 3.53 MiB on Linux.

Short-lived deep goroutines were the main cost. Their median paired time ratios were 1.37 on macOS and 2.18 on Linux. Each promotion creates an OS mapping and each exit releases it. The allocator currently creates a fresh mapping for every promotion. Most timed paths finish when the workload receives its last result, so a goroutine's final scheduling and teardown can extend beyond the measured interval. The repeated-growth Linux timings varied widely even with a fixed number of collections.

The measurements support keeping pooled small stacks and show that avoiding subsequent copies can help a deep stack on macOS. The promotion cost depends on the workload and OS. This run covers POSIX demand paging on Arm64. Windows uses the copying fallback, so it provides no performance measurement of `MEM_COMMIT`.
