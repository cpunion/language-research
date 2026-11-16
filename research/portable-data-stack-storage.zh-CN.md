# 可跨平台的数据栈存储

[English](portable-data-stack-storage.md) | 简体中文

调研日期：2026-11-16。本文依据文档与源码分析，没有新增运行实测。Goose 实现描述固定于 `32a4f445d8a94b22874a28469f95f69ba3ac31dd`。

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
