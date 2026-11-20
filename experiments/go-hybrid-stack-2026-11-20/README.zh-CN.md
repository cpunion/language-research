# Go 1.27.3 混合 goroutine 栈实验

[English](README.md)

这个实验把混合栈分配接进了真实 Go 运行时。goroutine 从现有栈池启动。栈增长目标达到 64 KiB 且不超过 1 MiB 时，运行时尝试把它迁入一段 1 MiB 的虚拟地址区。之后在这段地址区内扩缩容，栈帧地址保持不变。首次迁入和超出 1 MiB 后的搬迁都复用 Go 现有的复制路径，包括栈内指针和 channel 等待者的地址修正。

## 实现

- Go 原有的栈检查仍决定何时增长，目标容量仍按二次幂计算。地址区低端保留一个不可访问页，其余是按触页提供物理内存的可写匿名映射。原地增长只修改软件边界，无须系统调用。Linux 会尝试对该映射关闭透明大页。
- GC 沿用原有收缩条件，映射栈最小收缩到一个 OS 页。Linux 通过 `MADV_DONTNEED` 丢弃不用的完整页，macOS 在原地址重建匿名映射。以后再次访问时，内核按需提供清零后的页面。goroutine 退出时解除映射，普通栈仍走 Go 的栈池。
- 原型在每个 goroutine 的运行时描述结构中增加一个标记。`StackSys` 计入映射栈当前可用容量，进程 RSS 单独反映驻留。每个迁入的 goroutine 即使收缩到一页，仍占有 1 MiB 地址区和一个保护页。

源码覆盖构建固定为 Go 1.27.3。通过 `GODEBUG=hybridstack=1` 在 macOS 和 Linux 的 `arm64`、`amd64` 目标启用。实际运行测试在两边的 `arm64` 完成。另交叉编译了 `linux/amd64` 和使用复制路径的 `windows/amd64`。构建采用纯 Go。race、ASan、MSan、Valgrind、RuntimeSecret、分配追踪及依赖复制分配器的栈调试模式会跳过迁入。逻辑栈边界由 Go 原有的检查执行，硬件保护页位于整段预留区底端。[Go 栈实现](https://github.com/golang/go/blob/go1.27.3/src/runtime/stack.go) · [Go OS 内存接口](https://github.com/golang/go/blob/go1.27.3/src/runtime/mem.go)

## 复现与验证

在受支持的 macOS 或 Linux 上使用 Go 1.27.3 和 Python 3.9 以上版本。

```sh
python3 build.py -o /tmp/go-hybrid-stack
python3 run.py /tmp/go-hybrid-stack -o results.json
```

`build.py --goos linux --goarch arm64` 可生成 Linux 探针，在目标环境运行 `run.py` 即可得到本地结果。同一可执行文件分别关闭和开启混合策略。两边都使用 `GOMAXPROCS=4` 并关闭自适应初始栈容量。预热后，每种负载交替运行 31 对，校验输出一致。耗时在 Go 进程内记录，峰值 RSS 在 macOS 取自 `wait4`，在 Linux 取自 `/proc/self/status` 的 `VmHWM`。

正确性测试验证栈从 2 KiB 启动、64 KiB 迁入、原地增长到 512 KiB、收缩到一页并在原地址再次增长。触及 2 MiB 时回到复制栈，旧映射得到释放。测试保留跨递归帧的指针，在 goroutine 等待 channel 时多次执行 GC，用弱引用确认它持有的对象仍存活。还在启用执行追踪和 `gccheckmark=1` 的模式下重复验证。两个 Arm64 目标启用混合策略后均通过所选的 18 项 Go 运行时自带测试。[复现命令](README.md#reproduce)

## 实测

[macOS 27.0.1 原始数据](darwin-arm64.json) · [OrbStack Linux 7.0.14 原始数据](linux-arm64.json)。两边都在同一台 Apple M4 Max 上运行，OS 页分别为 16 KiB 和 4 KiB。递归基准每层保留一个 4 KiB 局部数组。反复扩缩容负载显式触发 GC，其他负载关闭自动 GC。

| 负载 | macOS 复制 → 混合 | Linux 复制 → 混合 |
|---|---:|---:|
| 16,384 个浅栈 goroutine | 7.46 → 7.50 ms | 10.00 → 9.73 ms |
| 单个 goroutine，201 层递归 | 0.180 → 0.094 ms | 0.148 → 0.168 ms |
| 512 个短命 goroutine，每个递归 81 层 | 4.89 → 6.39 ms | 8.72 → 18.89 ms |
| 同一 goroutine 增长 8 轮，每轮执行 4 次 GC | 3.65 → 3.35 ms | 45.00 → 46.33 ms |

表中是各模式的耗时中位数。单个深栈的逐对耗时比中位数在 macOS 为 0.55，在 Linux 为 1.14，Linux 样本波动范围有明显重叠。浅栈一直使用栈池，峰值 RSS 中位数在所示精度相同，macOS 为 48.61 MiB，Linux 为 46.45 MiB。单个深栈的峰值 RSS 中位数在 macOS 从 6.86 降到 5.38 MiB，在 Linux 从 4.74 降到 3.53 MiB。

最大的开销出现在大量短命深栈，逐对耗时比中位数在 macOS 为 1.37，在 Linux 为 2.18。每次迁入都创建一个 OS 映射，退出时解除。多数负载在收到最后一项结果时停止计时，最后几个 goroutine 的退出可能稍晚。Linux 反复扩缩容的耗时即使在 GC 次数固定时也有较大波动。

这组结果支持保留浅栈池。深栈在 macOS 上避开后续复制后缩短了耗时，迁入的成本取决于操作系统和栈的使用方式。实测覆盖 Arm64 上的 POSIX 按需分页。Windows 原型走复制路径，`MEM_COMMIT` 性能尚无实测数据。
