# Portable storage for growing data stacks

English | [简体中文](portable-data-stack-storage.zh-CN.md)

Research date: 2026-11-16. This is a source-based design assessment, without new runtime measurements. Goose-specific implementation details refer to `32a4f445d8a94b22874a28469f95f69ba3ac31dd`.

**Reserving addresses separately from physical backing is an established way to implement stable, growing arenas.** It is practical on Windows, Linux, and macOS. A portable language that also targets microcontrollers needs additional storage backends: hardware without address translation cannot provide the same sparse, contiguous virtual reservation through an allocator alone.

## Reservation, backing, and residency

Keep three quantities separate: the virtual address range reserved for growth, storage commitment/accounting where the OS has that concept, and pages currently resident in RAM. Reserving a large range does not allocate a matching amount of data RAM, but reservation metadata and address space are finite. Commitment is not necessarily residency, and successful reservation does not guarantee every future growth will succeed.

For a stack with page size `P`, reclaimable tail pages begin at `align_up(top, P)`, assuming the allocator has retained everything below `top`, including live capacity and metadata. A partial live page stays. The virtual range can remain reserved while its unused backing is discarded, so future growth uses the same addresses. The OS still chooses physical frames; address stability does not imply stable physical placement.

Moving `top` backward alone does not notify the kernel. The allocator must issue a discard/decommit operation if it wants backing released, or retain pages for reuse. Discarded bytes are no longer valid application data. A region manager must establish that no valid reference, asynchronous operation, or device still needs them.

## Existing operating-system mechanisms

- **Windows:** `VirtualAlloc(..., MEM_RESERVE, ...)` reserves addresses; `MEM_COMMIT` charges backing storage, while physical pages are obtained on access. `VirtualFree(..., MEM_DECOMMIT)` releases commitment while preserving reservation. Growth after decommit requires recommit, explicitly or through an installed runtime fault handler. [VirtualAlloc](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualalloc) · [VirtualFree](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualfree)
- **Linux:** private anonymous `mmap` supports demand-backed storage. One strategy maps it readable/writable; another first reserves `PROT_NONE` and enables subranges with `mprotect`. For ordinary private anonymous mappings, `madvise(..., MADV_DONTNEED)` discards contents and permits zero-filled demand-backed reuse without giving up addresses. `MADV_FREE` allows delayed reclamation, with different content and residency behavior. Page discard and access protection are separate decisions. [mmap](https://man7.org/linux/man-pages/man2/mmap.2.html) · [madvise](https://man7.org/linux/man-pages/man2/madvise.2.html)
- **macOS:** anonymous mappings also support this architecture, but Linux advice flags must not be assumed to have identical semantics. The pinned Goose backend replaces the unused range with a fresh anonymous `MAP_FIXED` mapping; this preserves its address while discarding its prior backing. Such replacement must be confined to a page-aligned range already owned exclusively by the allocator. [Apple mmap manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/mmap.2.html) · [Goose discard implementation](https://github.com/aardappel/goose/blob/32a4f445d8a94b22874a28469f95f69ba3ac31dd/src/runtime/runtime_impl.h#L665)

Goose already uses these three discard paths. `os.trim_memory()` returns unused complete tail pages; ordinary scope rewind retains them. Its source explicitly chooses this policy to avoid repeated page release and reacquisition for temporary work. A separate slice-pool trim can discard complete pages inside explicitly tracked free spans, even below the stack top. Neither facility automatically discovers dead objects. [Stack trim](https://github.com/aardappel/goose/blob/32a4f445d8a94b22874a28469f95f69ba3ac31dd/src/runtime/runtime_impl.h#L685) · [Slice-pool trim](https://github.com/aardappel/goose/blob/32a4f445d8a94b22874a28469f95f69ba3ac31dd/src/runtime/runtime_os.h#L1264)

## Go goroutine stacks versus reserved-address stacks

Checked against the standard Go runtime source on 2026-11-16. Stack capacity is not increased by one OS page at a time: `stackMin` is 2 KiB, `fixedStack` adds platform overhead and rounds to a power of two (8 KiB on Windows), and `newstack` normally doubles capacity, repeating if needed for a large frame. New goroutines can start above the minimum through adaptive sizing. Small stacks come from runtime pools; stack capacity, runtime allocation spans, and OS pages are distinct units. [Go stack implementation](https://go.dev/src/runtime/stack.go)

A reserved-address alternative would place the initial stack near the high end of its reserved range and map/commit lower pages as the downward-growing stack needs them. Within that reservation it can preserve addresses and avoid stack-copy pointer adjustment. Windows thread/fiber stacks already use reserved and initially committed storage, with additional pages committed on demand. A custom goroutine runtime still needs its own correct growth and fault/probing integration. [Windows thread stack size](https://learn.microsoft.com/en-us/windows/win32/procthread/thread-stack-size)

The following are design tradeoffs, not measured speedups or a claim about the Go team's rationale:

- Repeated growth of deep stacks can benefit from avoiding live-byte copying and pointer fixups. For monotonic doubling from an initial capacity to `C`, however, the sum of old capacities is less than `C`; copied live bytes are no greater than that sum. This bound excludes shrink/regrow cycles, metadata work, and pointer adjustment costs. A shallow stack that never grows offers no copy cost to remove.
- Per-stack page ownership can cost more RAM for small stacks. If a stack needs 2 KiB but independently occupies a 4 KiB page, its payload occupies twice as much physical capacity as packed 2 KiB stacks; metadata and actual residency remain separate. Page sizes and platform overhead differ, so this is a conditional comparison, not a universal multiplier. Both strategies can incur first-touch faults.
- A hypothetical million stacks reserving 1 MiB each use about 1 TiB of virtual space, not RAM. This can be feasible on a 64-bit target. Reservation size still bounds in-place growth, and sparse page tables, mapping metadata, protection boundaries, and 32-bit address limits need accounting. Bulk reservation and pooling can reduce mapping operations; a syscall for every goroutine or every small growth is not mandatory.
- Commit/decommit, zeroing, faults, and mapping/TLB changes have costs. Batched growth and a retained margin can reduce churn, while aggressive trimming reduces resident or committed storage. A stable address does not make growth latency constant. Exhausting the reservation still requires failure, relocation, or a different representation.

Avoiding stack relocation does not remove stack maps needed by tracing GC. Replacing Go's growth mechanism also needs to preserve scheduling behavior: its stack-guard path carries preemption requests as well as capacity checks. VM-backed fixed-address stacks are therefore a promising optional backend for a 64-bit MMU runtime, with bounded or segmented alternatives on constrained targets, rather than a demonstrated universally faster replacement.

Compare equal workloads and peak concurrency, separating tiny never-growing goroutines, long-lived deep stacks, and repeated deep/shallow phases. Measure total execution and tail latency, creation/destruction costs, copied bytes and pointer-fixup time, faults and VM calls, payload/stack capacity, RSS, commitment where applicable, page-table memory, and reserved virtual space. Keep batching, pooling, and trimming policies explicit.

## Embedded targets have two different cases

An embedded Linux system with a suitable MMU and VM configuration can use the Linux backend. An RTOS with supported translation/mapping facilities can implement a VM backend too, but capabilities must be checked per target. Zephyr requires enabled MMU support for virtual memory; its demand paging is a separate configuration, with architecture and backing-store requirements. Basic VM support does not imply arbitrary oversubscription of physical memory. [Zephyr virtual memory](https://docs.zephyrproject.org/latest/kernel/memory_management/virtual_memory.html) · [Demand paging](https://docs.zephyrproject.org/latest/kernel/memory_management/demand_paging.html)

On a target without address translation, reserving a contiguous address interval cannot make arbitrary physical blocks appear there later. A conventional MPU provides access protection rather than this translation. An allocator can still provide cheap growth and rewind using actual RAM, but cannot guarantee all of sparse reservation, stable contiguous raw addresses, and arbitrary shared-pool growth. [Arm virtual and physical addressing](https://support.arm.com/documentation/ddi0406/c/Application-Level-Architecture/Application-Level-Memory-Model/Virtual-and-physical-addressing)

Two practical no-MMU backends are:

- **Bounded contiguous arena:** use a caller-provided buffer or statically budgeted RAM. Allocation bumps a pointer; rewind makes capacity reusable. It preserves contiguous spans and stable addresses within the budget. Dedication of the buffer consumes actual addressable RAM capacity, even before initialization; clearing it later does not create spare backing for unrelated arenas automatically.
- **Segmented arena backed by a shared block pool:** obtain blocks as a stack grows, and return unused tail blocks on rewind. Existing blocks remain in place, so individual objects that fit in a block can retain stable addresses. Separate stack capacities need not all be provisioned at their peak simultaneously, but simultaneously live demand must fit the shared pool. A growing logical array may span blocks and therefore needs indexed/chunked access instead of unrestricted raw pointer arithmetic across its full length.

Pool blocks are software units, not necessarily hardware pages. Returning one makes it available to other arenas; it does not mean returning RAM to an operating system. Both backends can operate without a general-purpose heap, given supplied memory and bounded metadata.

## A portable language-level contract

Use common lifetime operations such as allocate, mark, rewind, and trim, with storage capabilities made explicit. VM-backed contiguous storage, bounded buffers, and segmented storage can share lifetime semantics while differing in representation and growth limits.

Contiguity must be part of the container and FFI design. A logical array can support iteration and indexing on every backend, while a contiguous span is available only for one physical/virtual interval. A span crossing segment boundaries needs a different API or an explicit contiguous copy. Keeping each allocated object contiguous may require large-object blocks. An allocator-interface substitution alone cannot preserve every packed-array and interior-pointer guarantee of Goose on a segmented backend.

Suggested release policy: ordinary rewind retains a bounded amount of recently used storage; explicit trim, large drops, idle periods, or budget pressure trigger release of complete unused pages/blocks. The retained margin and thresholds need measurement. Immediate release on every shrink adds system calls, mapping/TLB work, and future page faults or zeroing. Real-time tasks should use budgeted storage acquired and made resident before the critical interval; demand paging does not offer constant access latency.

This storage layer addresses backing and reuse. It does not decide which objects have died, reclaim a page containing still-needed bytes, or replace the lifetime analysis and GC responsibilities described in the [hybrid memory study](goose-hybrid-gc.md).
