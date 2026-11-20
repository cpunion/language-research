# 可跨平台的数据栈存储

[English](portable-data-stack-storage.md) | 简体中文

初稿日期为 2026-11-16，2026-11-20 补入栈存储实测。Goose 实现描述固定于 `32a4f445d8a94b22874a28469f95f69ba3ac31dd`。

**先预留地址、再按需配置物理存储，是实现地址稳定、可增长 arena 的成熟方式。** Windows、Linux、macOS 都能实现。若语言还要支持微控制器，则需要其他存储后端：没有地址转换的硬件，无法仅靠分配器提供相同的稀疏连续虚拟地址预留。

## 地址预留、存储承诺与物理驻留

需要区分三个数量：为增长预留的虚拟地址范围、操作系统上的存储承诺／记账，以及当前驻留 RAM 的页面。预留大范围不等于占用同样多的数据 RAM，但地址空间与映射元数据仍然有限。commit 不一定等于驻留；预留成功也不保证以后每次增长都能成功。

页面大小为 `P` 时，假定分配器已将全部有效容量和元数据计入 `top` 以下，则可回收的尾部完整页面从 `align_up(top, P)` 开始。包含存活数据的半页必须保留。丢弃尾部物理存储后，仍可保留虚拟地址范围，以后在原地址重新增长。物理页框由操作系统安排，虚拟地址稳定不表示物理地址稳定。

只将 `top` 后移，不会通知内核。需要分配器发出 discard／decommit 操作才能归还存储，也可以保留页面用于复用。被丢弃的内容已不是有效应用数据；区域管理器必须先确认引用、异步操作和设备都不再需要它。

## 已有的操作系统机制

- **Windows**：`VirtualAlloc(..., MEM_RESERVE, ...)` 预留地址；`MEM_COMMIT` 承担存储记账，物理页面在访问时取得。`VirtualFree(..., MEM_DECOMMIT)` 归还承诺存储，保留地址预留。重新增长时需要再次 commit，可以显式执行，也可由已安装的运行时异常处理器执行。[VirtualAlloc](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualalloc) · [VirtualFree](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualfree)
- **Linux**：私有匿名 `mmap` 支持按需取得页面。可以先建立可读写映射，也可以先用 `PROT_NONE` 占住地址，再通过 `mprotect` 开放子范围。对普通私有匿名映射，`madvise(..., MADV_DONTNEED)` 可以丢弃内容，保留映射，之后按需获得零填充页面。`MADV_FREE` 允许延迟回收，内容与驻留行为不同。丢弃页面与禁止访问是两项独立操作。[mmap](https://man7.org/linux/man-pages/man2/mmap.2.html) · [madvise](https://man7.org/linux/man-pages/man2/madvise.2.html)
- **macOS**：匿名映射同样可以支持这种架构，但不能假设 Linux 的 advice 标志具有相同效果。固定版本的 Goose 将不用的范围以 `MAP_FIXED` 替换为新的匿名映射，在原地址丢弃旧存储。这种替换必须限定在分配器独占、已经拥有且按页对齐的范围内。[Apple mmap 手册](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/mmap.2.html) · [Goose 丢弃页面实现](https://github.com/aardappel/goose/blob/32a4f445d8a94b22874a28469f95f69ba3ac31dd/src/runtime/runtime_impl.h#L665)

Goose 已经使用上述三种路径。`os.trim_memory()` 归还尾部不用的完整页面，普通作用域回退则保留页面。源码明确说明，这样便于临时工作区复用，避免反复释放和重新取得页面。另外，切片池的 trim 可以回收被空闲范围元数据明确记录的完整页面，包括栈顶以下的空闲范围；两者都不会自动发现死对象。[栈 trim](https://github.com/aardappel/goose/blob/32a4f445d8a94b22874a28469f95f69ba3ac31dd/src/runtime/runtime_impl.h#L685) · [切片池 trim](https://github.com/aardappel/goose/blob/32a4f445d8a94b22874a28469f95f69ba3ac31dd/src/runtime/runtime_os.h#L1264)

## Go goroutine 栈与预留地址栈

于 2026-11-16 核对标准 Go 运行时源码。栈容量不是每次增加一个 OS 页面：`stackMin` 为 2 KiB，`fixedStack` 加上平台开销后向上取二次幂，Windows 为 8 KiB；`newstack` 通常将容量翻倍，大栈帧可能要求连续翻倍。自适应机制也可以让新 goroutine 从更大的栈起步。小栈来自运行时池，栈容量、运行时分配 span 和 OS 页面是不同单位。[Go 栈实现](https://go.dev/src/runtime/stack.go)

预留地址的替代方案可以在预留区高端放置初始栈，向低地址增长时，映射／提交后续页面。在预留范围内可以保持地址稳定，避免复制栈和修正指针。Windows 的线程／fiber 栈已经使用预留地址与初始提交存储，后续按需提交页面。自定义 goroutine 运行时仍需正确集成增长和异常／探测协议。[Windows 线程栈说明](https://learn.microsoft.com/en-us/windows/win32/procthread/thread-stack-size)

以下是设计取舍，不是实测加速比，也不声称是 Go 团队选择现有方案的官方理由：

- 深栈反复增长时，避免复制有效字节和修正指针可能有利。不过，从初始容量单调翻倍到 `C` 时，历次旧容量之和小于 `C`，复制的有效字节不会超过这个和。此界限不包含缩小再增长、元数据处理和指针修正成本。始终不增长的小栈则没有复制成本可以消除。
- 每个栈独占页面，可能提高小栈的 RAM 开销。如果仅需 2 KiB 的栈独占一个 4 KiB 页面，载荷占用的物理容量就是紧凑存放 2 KiB 栈的两倍；元数据和实际驻留另外计算。页大小与平台开销不同，这是条件比较，不是通用倍率。两种方案都可能产生首次触页缺页。
- 假设一百万个栈各预留 1 MiB，总虚拟地址约为 1 TiB，不是 RAM；在 64 位目标上可能可行。但预留大小仍限制原地增长，稀疏页表、映射元数据、保护边界和 32 位地址上限需要计入。整批预留与池化可以减少映射操作，不必每创建一个 goroutine 或每次小幅增长都调用 OS。
- commit／decommit、清零、缺页、映射和 TLB 变化都有成本。按批次增长、保留余量可以减少反复操作，积极 trim 则降低驻留或承诺存储。地址固定不保证增长延迟恒定；超过预留上限时，仍需失败、搬迁或改用其他表示。

避免栈搬迁，不会取消追踪 GC 所需的栈映射。替换 Go 的增长机制时还需保留调度行为，因为栈 guard 路径同时承担抢占请求和容量检查。因此，VM 固定地址栈值得作为 64 位 MMU 运行时的可选后端，受限平台保留有界／分块方案；尚不能视为已证明普遍更快的替代品。

同一台 Apple M4 Max 上的 C 存储原型对照共进行了 225 次独立运行。每个栈帧为 512 字节，带有指向前一帧的引用。所有实现使用相同输入，并校验内容、引用与复制量。[实验实现与数据](../experiments/stack-growth-2026-11-20/README.md)

- **16,384 个浅栈**。macOS 上密集存放的 2 KiB 小栈耗时中位数为 2.43 ms，采样 RSS 为 34.2 MiB。按需映射为 40.35 ms 和 258.2 MiB。Linux 的基础页为 4 KiB，但默认按需映射的 RSS 仍到了 257.8 MiB。仅对此映射关闭透明大页后，RSS 降为 65.8 MiB。
- **256 个栈一次增长到各 256 KiB**。按需映射省去 63.5 MiB 复制和 128,256 次链接修正。macOS 中位数由 8.37 降到 7.58 ms，Linux 从 11.62 降到 8.75 ms。macOS 样本范围重叠。每次通过 `mprotect` 开放页面的版本在两边都没有快过复制基线。
- **反复增长和回退 16 轮**。按需映射每轮 trim 到最小容量时，macOS 的总时间从 48.15 升到 137.89 ms，最终采样 RSS 从 65.7 降到 5.7 MiB。Linux 的总时间从 87.49 升到 145.78 ms，最终采样 RSS 从 65.3 降到 2.3 MiB。频繁还页引入了下一轮触页成本，模型适合分别评估保留容量和主动归还两种策略。

这些数据来自模拟栈帧的存储实验。RSS 是阶段采样值，可能漏掉复制过程中的瞬时峰值。

后续的 [Go 1.27.3 运行时原型](../experiments/go-hybrid-stack-2026-11-20/README.zh-CN.md) 保留小栈池，栈达到 64 KiB 时迁入虚拟地址区。测试验证了原地增长、GC 收缩及再次增长期间的地址稳定性，超过 1 MiB 后回到复制路径。每种负载运行 31 对。单个深栈在 macOS 上的耗时中位数由 0.180 降到 0.094 ms，Linux 从 0.148 升到 0.168 ms，样本有重叠。大量短命深栈在两边都变慢。原型为每个迁入的栈单独预留地址，退出时解除映射。Windows `MEM_COMMIT` 性能尚无实测数据。

## 嵌入式有两种不同情况

具有适当 MMU 与虚拟内存配置的嵌入式 Linux，可以使用 Linux 后端。具有相应地址转换和映射能力的 RTOS 也可以移植 VM 后端，但要逐目标核对能力。例如 Zephyr 的虚拟内存要求启用 MMU，按需分页还需要单独配置及架构、后备存储支持。基础 VM 支持不表示可以任意超配物理内存。[Zephyr 虚拟内存](https://docs.zephyrproject.org/latest/kernel/memory_management/virtual_memory.html) · [按需分页](https://docs.zephyrproject.org/latest/kernel/memory_management/demand_paging.html)

没有地址转换的目标，不能预留一段连续地址，再让任意物理块映射到那里。常规 MPU 提供访问保护，不提供这类转换。分配器仍然能在真实 RAM 上廉价增长与回退，但不能同时保证稀疏预留、连续稳定的原始地址，以及从共享池任意扩容。[Arm 虚拟与物理地址说明](https://support.arm.com/documentation/ddi0406/c/Application-Level-Architecture/Application-Level-Memory-Model/Virtual-and-physical-addressing)

两种可行的无 MMU 后端：

- **有界连续 arena**：使用调用者提供的缓冲区，或静态预算的 RAM。分配推进指针，回退后容量可以复用，在预算内保持连续与地址稳定。划给缓冲区的是实际可寻址 RAM 容量，即使尚未初始化也不能同时专用于其他区域；清空不会自动产生可供其他 arena 使用的物理存储。
- **共享块池上的分块 arena**：数据栈增长时领取块，回退时归还不用的尾块。旧块不搬移，能放进单块的独立对象可以保持地址稳定。不同数据栈无需各自预备峰值容量，但同时存活的数据必须放得进共享池。增长中的逻辑数组可能跨块，需要索引／分块访问，不能对整个数组任意使用原始指针算术。

池中的块是软件管理单位，不一定是硬件页面。归还块表示其他 arena 可以使用，并不表示将 RAM 归还操作系统。如果提供了内存与有界元数据，这两种后端都可以不依赖通用堆。

## 可移植的语言层契约

统一 allocate、mark、rewind、trim 等寿命操作，并明确存储能力。VM 连续存储、有界缓冲区、分块存储可以共享寿命语义，但布局与增长上限不同。

容器与 FFI 必须参与连续性设计。逻辑数组在所有后端都可以支持迭代和索引；连续 span 则只能覆盖一个实际／虚拟连续区间。跨块 span 需要不同接口，或显式复制成连续缓冲区。若要求每个分配对象连续，还需要大对象块。仅替换分配器接口，不足以在分块后端保持 Goose 的全部紧凑数组与内部指针保证。

建议策略：普通回退保留有上限的近期存储；显式 trim、大幅收缩、空闲时段或预算压力触发完整页面／块的归还。保留余量和阈值需要测量。每次收缩立即归还，会增加系统调用、映射／TLB 工作，以及下一次增长的缺页或清零成本。实时任务应在关键时段前取得预算内存并使之驻留，按需分页不能保证恒定访问延迟。

这一层解决物理存储和复用问题。对象何时死亡、含有效字节的页面能否回收，仍需要寿命分析及[混合内存模型](goose-hybrid-gc.zh-CN.md)中的其他机制。
