# Copying versus growing a stack in place

[中文结果解读](../../research/portable-data-stack-storage.zh-CN.md#go-goroutine-栈与预留地址栈)

This experiment measures stack storage in isolation. Each synthetic frame occupies 512 bytes and has a pointer to the preceding frame. Every case writes the same frame contents and walks the same linked frames to verify contents after growth and rewind. The runner checks checksums and exact counts of copied bytes, adjusted pointers, and resize operations. It models Go's doubling and relocation but does not run a modified Go scheduler or GC. [Go runtime stack source](https://go.dev/src/runtime/stack.go)

- `copy` packs 2 KiB initial slots in a shared slab. It obtains larger buffers from `malloc`, doubles capacity, copies live frames, and repairs links. A trim copies the two live frames back to the initial slot and frees the large buffer through libc. libc decides when to release backing memory.
- `vm-protect` reserves one `PROT_NONE` mapping partitioned into 1 MiB slots. Each stack starts with one writable base page. It doubles writable capacity via `mprotect`, and a trim replaces the unused suffix with a fresh `PROT_NONE` mapping.
- `vm-lazy` makes one anonymous read/write mapping and partitions it into the same slots. First writes obtain backing from the OS. Growth updates the logical capacity, and trim discards tail pages via `MADV_DONTNEED` on Linux or a fresh anonymous `MAP_FIXED` mapping on macOS. The benchmark bounds-checks against the slot size in software. A production runtime would need its own stack bounds and fault/probing policy. [Linux madvise](https://man7.org/linux/man-pages/man2/madvise.2.html) · [Apple mmap](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/mmap.2.html)
- `vm-lazy-nohuge` adds one `MADV_NOHUGEPAGE` call to the Linux shallow case. This tests the contribution of the guest kernel's 16 KiB multi-size transparent huge pages. [Linux kernel documentation](https://docs.kernel.org/admin-guide/mm/transhuge.html)

`shallow` creates 16,384 stacks and uses 1 KiB in each. `deep` fills 256 stacks once, gradually growing each to 256 KiB. `cycle-keep` and `cycle-trim` repeat the deep fill 16 times, rewinding each stack to 1 KiB after each wave. The former retains capacity and the latter returns to the minimum. A trim all the way to minimum is a stress policy and is more aggressive than Go's usual halving rule. The VM reservation limits every modeled stack to 1 MiB, and the copy baseline uses the same limit for this comparison.

## Measurements

Each cell below is median milliseconds over nine fresh processes. Timing includes setup, fills, verification, rewinds and teardown. It excludes process startup and RSS sampling. The platforms used the same Apple M4 Max host. macOS 27.0.1 ran natively with Clang 22.1.8 and 16 KiB base pages. Linux ran as an Arm64 Ubuntu 26.04.1 container in OrbStack with GCC 15.2.0 and 4 KiB base pages. Compare variants within each environment.

| Workload | macOS copy | macOS protected | macOS lazy | Linux copy | Linux protected | Linux lazy |
|---|---:|---:|---:|---:|---:|---:|
| Shallow | 2.43 | 55.85 | 40.35 | 3.72 | 47.71 | 23.40 |
| Deep | 8.37 | 9.60 | 7.58 | 11.62 | 11.80 | 8.75 |
| Cycle, retain | 46.85 | 49.90 | 48.15 | 77.85 | 88.43 | 87.49 |
| Cycle, trim to minimum | 72.72 | 146.90 | 137.89 | 102.58 | 182.78 | 145.78 |

- **Shallow stacks benefit from dense packing.** The copy baseline's sampled RSS was 34.2 MiB on macOS and 33.8 MiB on Linux. The lazy VM layout used 258.2 and 257.8 MiB. The Linux guest enabled 16 KiB multi-size THP. Disabling THP for just that mapping reduced RSS to 65.8 MiB and the median time to 15.57 ms. The large virtual reservation was 16 GiB in each shallow VM case.
- **One cold growth favors the lazy mapping on this workload.** The copy variant moved 63.5 MiB and adjusted 128,256 frame links. Lazy VM avoided both and reduced median elapsed time by 9% on macOS and 25% on Linux. The macOS sample ranges overlap. Explicitly enabling pages with `mprotect` did not improve the median over copy on either platform.
- **Eager trim trades reuse for lower resident memory.** With lazy VM, cycle times rose from 48.15 to 137.89 ms on macOS and from 87.49 to 145.78 ms on Linux. Final sampled RSS fell from 65.7 to 5.7 MiB and from 65.3 to 2.3 MiB, respectively. Work-phase fault counts rose from 4,096 to 61,696 on macOS and to 73,216 on Linux. The copy baseline with trim ended at about 138.4 MiB on macOS and 2.6 MiB on Linux. The baseline uses different libc allocators, so `free` in this harness cannot stand in for Go's stack scavenging policy.

RSS is sampled process RSS before teardown, including process overhead, with `task_info` on macOS and `/proc/self/statm` on Linux. The latter can report approximate counters. `full_rss` is the largest sample taken after a fill or during setup. It can miss transient peaks during a copy. Fault counts are process totals over the workload, with Mach task events on macOS and `getrusage` on Linux. The runner reports min/max timings and keeps per-wave times, which are batch timings rather than per-goroutine tail latency. Windows `MEM_COMMIT`, bare metal, scheduler costs, kernel page table memory, and GC scanning were outside this experiment.

## Reproduce

From the repository root, run the benchmark with an installed C compiler and Python 3.

```sh
python3 experiments/stack-growth-2026-11-20/run.py --cc clang --output /tmp/stack-growth.json
```

On Linux, add `--linux-nohuge` for the shallow control. Recorded results include the source hash, compiler, page size, Linux THP settings, and individual samples. [macOS results](results-macos-arm64.json) · [Linux results](results-linux-arm64.json)
