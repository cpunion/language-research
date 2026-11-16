# LLGo 嵌入式目标与虚拟内存能力

[English](llgo-embedded-memory.md) | 简体中文

调研日期：2026-11-16。本地检出的路径实际为 `~/source/goplus/llgo`，检查版本为 [`0f804925c56d9f756e5e4b25271662157799a05a`](https://github.com/cpunion/llgo/tree/0f804925c56d9f756e5e4b25271662157799a05a)。本文检查配置与硬件资料，没有新增开发板构建或实机测试，也没有修改 LLGo 工作区。

**这些目标中的大部分 MCU，无法仅靠操作系统或分配器获得透明、通用的 RAM 分页。** 部分目标有受限的外部存储转换；K210 有非标准 MMU，Cortex-A57 则有常规 MMU。硬件能力、固件配置、运行时集成与可用的 VM 分配接口，需要分别核实。

## 目标清单的范围

检查版本的 `targets/` 有 210 个 JSON 配置，包含开发板别名、CPU 模板、配置混入项，以及五个 WebAssembly 配置。递归解析父配置的 CPU 与 build tags 后，可分成：Cortex-M 142、AVR 18、ESP32 9、ESP32-C3 10、ESP8266 3、FE310 2、K210 2；Game Boy Advance、TKey、Nintendo Switch、RISC-V QEMU 各一；另有 15 个通用／混入配置和五个 Wasm 配置。这是配置数量，不是不同芯片数或实测通过的开发板数量。[目标目录](https://github.com/cpunion/llgo/tree/0f804925c56d9f756e5e4b25271662157799a05a/targets)

构建脚本跳过了部分目标，并将某些缺少 CPU／triple 的情况计为 warning。存在配置文件，不足以证明编译器、运行时、开发板或 VM 已可用。通用 `riscv32`／`riscv64` 名称和 LLVM feature 字符串，不能决定具体芯片是否包含 MMU。同样，裸机配置中的 `linux` build tag 不证明运行了 Linux 或存在 `mmap`。[构建覆盖脚本](https://github.com/cpunion/llgo/blob/0f804925c56d9f756e5e4b25271662157799a05a/_demo/embed/targetsbuild/build.sh) · [目标继承实现](https://github.com/cpunion/llgo/blob/0f804925c56d9f756e5e4b25271662157799a05a/internal/targets/loader.go)

## 没有通用分页 MMU 的家族

- **Cortex-M0／M0+／M3／M4／M7／M33**：配置包括 RP2040、RP2350；nRF51／nRF52／nRF52833／nRF52840；SAMD21／SAMD51／SAME51／SAME54；所列 STM32 F1／F4／F7／L0／L4／L5／WL 型号；以及采用 MK66／i.MX RT1062 的 Teensy 3.6／4.0／4.1。Cortex-M 的内存保护功能在实现时也不提供 CPU 虚拟页到物理页转换，TrustZone 不会补出该功能。这里判断的是通用 RAM 分页，不是否认 SoC 可能另有存储窗口。[Arm 说明](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/security-principles-for-trustzone-for-armv8_2d00_m-webinar) · [RP2040／RP2350 等目标配置](https://github.com/cpunion/llgo/tree/0f804925c56d9f756e5e4b25271662157799a05a/targets)
- **AVR ATmega／ATtiny**：具体配置包括 ATmega1280／1284P／2560／328P／328PB／32U4、ATtiny85／1616，以及板级别名和 AVR 模拟器。原生数据访存不经过通用页表 MMU。[ATmega328P 代表性内存架构](https://ww1.microchip.com/downloads/en/DeviceDoc/Atmel-7810-Automotive-Microcontrollers-ATmega328P_Datasheet.pdf)
- **SiFive FE310／HiFive1 Rev B**：E31 提供物理内存保护，不提供 Sv32 虚拟内存。FE310-G002 手册第 3.6–3.7 节描述的是 M／U 特权模式和 PMP。RISC-V 规范定义了虚拟内存，不意味着该实现具有转换硬件。[SiFive 手册](https://sifive.cdn.prismic.io/sifive/034760b5-ac6a-4b1c-911c-f4148bb2c4a5_fe310-g002-v1p5.pdf)
- **Game Boy Advance**：所配置的 ARM7TDMI 平台没有通用 MMU。仅凭 ARM7TDMI 核心名称不能推断所有 SoC：Arm 曾将该核心与额外 MMU 集成到其他产品中。[LLGo 配置](https://github.com/cpunion/llgo/blob/0f804925c56d9f756e5e4b25271662157799a05a/targets/gameboy-advance.json) · [Arm 核心特征比较](https://documentation-service.arm.com/static/5ea04d399931941038de55af)
- **TKey**：其基于 PicoRV32 的 FPGA CPU 不实现 MMU 指令，厂商还说明了受限的 CSR、特权与中断能力。重新设计 FPGA 逻辑可以在资源允许时增加功能，但更换操作系统本身不会增加硬件。[TKey 硬件手册](https://dev.tillitis.se/castor/hw/)

RP2350 需要额外说明：QMI 外设有八个 XIP 地址转换项，对应 4 MiB 窗口，芯片也支持外接 PSRAM。这是受限的外部存储窗口，不是通用 SRAM 页表。专用分配器可以研究如何利用，但本文没有证明任意独立页面映射或完整按需分页 arena 已可实现。[Raspberry Pi QMI 寄存器](https://github.com/raspberrypi/pico-sdk/blob/master/src/rp2350/hardware_structs/include/hardware/structs/qmi.h) · [芯片内存比较](https://www.raspberrypi.com/documentation/microcontrollers/microcontroller-chips.html)

## 受限转换与完整 MMU 的例外

**不能准确地说 ESP32 没有 MMU。** ESP-IDF 提供外部存储映射。原版 ESP32 的 Himem 将外部 PSRAM 分配与地址窗口分配分开，可在有限的 4 MiB CPU 窗口内映射／解除映射 32 KiB bank。这与受限的预留／映射分配器直接相关，但不能将任意内部 SRAM 映射到巨大的稀疏地址空间。仍有有效使用者时，不能将 bank 改映射成无关数据。LLGo 集成和开发板所需 PSRAM 未在本文实测。[Himem](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/himem.html)

**ESP32-C3 也有用于外部映射的 MMU**，但检查的 ESP-IDF 映射驱动将 SPI flash 列为物理目标，这不能证明存在可写 RAM 页池后端。ESP8266 文档里的“MMU”配置涉及指令 RAM 与外部 flash 缓存容量，也不能据此推导通用 RAM 分页。[ESP32-C3 映射](https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-reference/system/mm.html) · [ESP8266 core 文档](https://github.com/esp8266/Arduino/blob/master/doc/mmu.rst)

**K210／Maix Bit 确实具有基于特权规范 1.9 草案的 Sv39 MMU。** 检查的 Linux 设备树明确说明主线不支持该草案，K210 仅在 NOMMU 情况下启用。专用兼容运行时可以研究已有硬件，这与补造不存在的硬件不同；也不表示 LLGo 已启用或使用其 MMU。[Linux K210 说明](https://github.com/torvalds/linux/blob/master/arch/riscv/boot/dts/canaan/k210.dtsi)

**Nintendo Switch 的 Cortex-A57 有常规 MMU**，但目标配置不证明其执行环境允许修改映射或提供适合的 VM API。**RISC-V QEMU `virt`** 可以模拟有 MMU 的 CPU，但它是虚拟平台，不能据此判断物理 MCU，具体 LLGo 固件仍需集成 VM。Wasm 和通用模板应另外评估。[Cortex-A57 MMU](https://support.arm.com/documentation/ddi0488/c/memory-management-unit) · [QEMU virt](https://www.qemu.org/docs/master/system/riscv/virt.html)

## 操作系统、第三方或新增硬件能否实现？

在具有适当地址转换机制的硬件上，操作系统可以实现物理页分配、页表、缺页处理及 reserve／map／discard 接口。系统可以很小；如果只丢弃死数据，并保证存活需求放得进 RAM，就不需要交换设备。在合适架构中，页表遍历或 TLB refill 本身可以由软件完成，但实际访存路径仍需能够应用转换。[RISC-V 地址转换与软件 refill](https://riscv.github.io/riscv-unified-db/manual/html/isa/isa_20240411/chapters/supervisor.html)

没有这条硬件路径时，软件页表只是普通数据。原生 `load [address]` 不会自动查询它。MPU／PMP 可以触发保护异常，但处理器重试指令时，仍不会产生硬件不支持的地址转换。模拟该指令、将数据复制进合适的已有物理窗口，或改写执行方式，都会改变机制和成本。

理论上可以软件模拟：由编译器将相关访存改写为块表／页表查询，或由解释器、模拟器转换 guest 访存；运行时也可以采用句柄加偏移引用。部分查询能被提升或缓存，但性能、ABI、原始指针身份、C／汇编访存和 DMA 都必须配套设计。这是架构选项，不是已测得的 LLGo 优化。[QEMU 区分 host 指针与 guest 虚拟地址的访问路径](https://www.qemu.org/docs/master/devel/loads-stores.html)

新增硬件只有放在实际访存路径上，才能转换那些访问。外接 SPI 设备不能拦截 CPU 对片内 SRAM 的普通访问。重新设计 SoC、FPGA 软核或合适的外部存储总线，可以加入转换逻辑，但这是受访存路径限制的硬件改造，不能作为适用于所有现有开发板的 OS 软件包。TKey 的 FPGA 很适合说明这一区别，但本文没有证明具备相应功能的设计放得进其 FPGA。

对拟议语言，应分别描述原生 VM、受限外部窗口和无 MMU 存储能力。常见 MCU 使用有界或分块 arena；ESP32 PSRAM 映射可以单独做实验。若探索软件转换引用，应将其成本与原生指针读取分别测量。检查版本的 LLGo 裸机 GC 从链接符号取得堆边界，`gcGrowMemory` 返回原边界，尚不是按需分页分配器。[裸机堆集成](https://github.com/cpunion/llgo/blob/0f804925c56d9f756e5e4b25271662157799a05a/runtime/internal/runtime/tinygogc/gc_link.go) · [可移植存储设计](portable-data-stack-storage.zh-CN.md)
