# Target profile: STM32 (STMicroelectronics, Arm Cortex-M, NVIC)

Read this before a board-specific STM32 file. It records what holds across the
Cortex-M cores STM32 parts are built on, and — more usefully — **how to find
out** the things that vary by part.

> **Provenance: mostly unmeasured.** Written from architecture references, then
> partly confirmed on 2026-09-20 against an **STM32F777ZIT6** (Meadow F7) over
> an ST-Link/V3M, OpenOCD 0.12.0+dev-02429. Items confirmed there are marked
> `(measured on F777)`; everything else is `(assumed)` and is a thing to check
> the first time it matters. The template's warning holds: a profile that reads
> as verified and is not is worse than no profile.
>
> A single part confirming an item does not make it true of the family. The
> constants that are architectural — memory layout, the exception-number
> offset, how to read the trigger counts — travel. Trigger counts, priority
> bits and what a fault handler does do not, which is why this file gives the
> procedure for reading them rather than the answer.

---

## Connection `(assumed)`

Probes, commonest first:

| Probe | OpenOCD | probe-rs | pyOCD |
|---|---|---|---|
| ST-LINK V2/V3 (on-board or standalone) | `-f interface/stlink.cfg -f target/stm32<fam>x.cfg` | `--chip STM32<PART>` | `--target stm32<part>` |
| J-Link | `-f interface/jlink.cfg -f target/stm32<fam>x.cfg` | same | same |

`interface/stlink.cfg` covers V2 and V3 alike — there is no per-version file.
OpenOCD names the probe it found on connect, which is the quickest confirmation
that the right one is attached `(measured on F777)`:

```
Info : STLINK V3J9M3 (API v3) VID:PID 0483:374E
Info : Target voltage: 3.244504
```

A **target voltage near 0** means the probe is attached but the board is not
powered, which otherwise presents as a target that cannot be examined.

**Clock speed**: `Unable to match requested speed 2000 kHz, using 1000 kHz` is
normal and harmless — the adapter picks the nearest it can divide to.

**`reset_config`**: ST-Link clones without the RST pin wired need
`reset_config none separate`. On a genuine ST-Link it can usually be left out;
if it is present and reset behaves oddly, that line is the first suspect.

The target cfg is per family, not per part: `target/stm32f7x.cfg`,
`target/stm32f4x.cfg`, `target/stm32h7x.cfg`. Getting the family right matters
more than the exact part; getting it wrong usually shows up as a flash driver
that cannot identify the device.

GDB is `arm-none-eabi-gdb`, or `gdb-multiarch` on a host that ships no Arm GDB.
Connect with `target extended-remote localhost:3333` — prefer the explicit host
over `:3333`, which resolves differently on some hosts.

**RTOS awareness**: OpenOCD will try to read RTOS thread state if the target cfg
asks it to. A mismatch between the configured RTOS and the firmware spams
warnings and slows every halt. Disable it (`$_TARGETNAME configure -rtos none`)
until threads are actually needed.

**Debug link and console**: an ST-LINK presents the debug link and a virtual COM
port as separate endpoints on one USB device. They are independent, so the
console can be held open across a reset — see `../SKILL.md` section 1b. This is
a real advantage over targets where the two share a UART; use it.

## Console `(assumed)`

- Application output normally goes to a USART wired to the ST-LINK VCP,
  conventionally 115200 8N1. Which USART and which pins is board-specific and
  belongs in the board file, not here.
- Reset non-interactively with `monitor reset halt` over the debug link rather
  than by toggling lines on the console port.

**The polled output path.** A `printf` through a DMA or interrupt-driven
driver is useless in a fault handler, because the thing that would carry it out
is exactly what has stopped. The path that survives busy-waits on the
transmit-empty flag.

Under NuttX it already exists and is called **`arm_lowputc()`**
`(measured on F777)`, in `arch/arm/src/stm32f7/stm32_lowputc.c`:

```c
void arm_lowputc(char ch)
{
  /* Wait until the TX data register is empty */
  while ((getreg32(STM32_CONSOLE_BASE +
                   STM32_USART_ISR_OFFSET) & USART_ISR_TXE) == 0);
```

It polls `USART_ISR_TXE` at `STM32_CONSOLE_BASE` and touches no driver state,
so it works with interrupts dead or the scheduler wedged. Report through it
from any handler. `up_putc()` sits on top of it.

**Semihosting is not that path.** It halts the core on every call. It works, it
is convenient, and it makes timing-dependent faults disappear. Do not use it to
investigate anything that depends on timing, and never leave it enabled in a
build that has to run without a debugger attached — the target stalls at the
first call with nothing to service it.

## Memory map and breakpoints `(assumed)`

Architecturally fixed across STM32:

| Region | Address | Breakpoints |
|---|---|---|
| Flash (XIP, aliased to 0x0 when booting from flash) | `0x08000000` | `hbreak` |
| SRAM | `0x20000000` | `break` works |
| Cortex-M7 ITCM (F7, H7) | `0x00000000` | see below |
| Cortex-M7 DTCM (F7, H7) | `0x20000000` | `break` works |
| Peripherals | `0x40000000` | — |
| Private peripheral bus (SCB, NVIC, FPB, DWT) | `0xE0000000` | — |

A software breakpoint in XIP flash usually fails silently: GDB reports it armed
and it never fires. Use `hbreak` for anything in `0x08000000`.

**Trigger counts vary by part — read them rather than recalling them:**

```
x/1wx 0xE0002000   # FP_CTRL  - FPB code comparators
x/1wx 0xE0001000   # DWT_CTRL - NUMCOMP in bits 31:28
```

`NUM_CODE` in `FP_CTRL` is **split across two fields**: bits 7:4 are the low
nibble, bits 14:12 the high bits. Reading bits 7:4 alone is right only for
parts with eight or fewer, and silently wrong above that.

Worked example `(measured on F777)`: `FP_CTRL = 0x10000080` → bits 7:4 = 8,
bits 14:12 = 0, so **8 breakpoints**; bits 31:28 = 1 means FPBv2.
`DWT_CTRL = 0x40000000` → bits 31:28 = 4, so **4 watchpoints**. OpenOCD prints
the same conclusion on connect (`target has 8 breakpoints, 4 watchpoints`), so
the registers are a cross-check rather than the only route.

On Cortex-M the two pools are separate — FPB for breakpoints, DWT for
watchpoints — so spending one does not cost the other.

**Cortex-M7 erratum 3092511** `(measured on F777)`. OpenOCD reports it on
connect:

```
Warn : Cortex-M7 can halt in an incorrect address when breakpoint and
       exception occurs simultaneously
Info : The erratum 3092511 workaround will resume after an incorrect halt
```

A breakpoint that appears to halt somewhere it was never set is this, not a
corrupt program counter. OpenOCD works around it by resuming; the cost is that
a halt can be missed rather than land wrong. Do not spend time explaining an
impossible halt address on a Cortex-M7 before ruling this out.

## Boot and reset `(assumed)`

- **BOOT0**, and on some parts option bytes, select what runs out of reset:
  flash, system memory (the factory bootloader), or SRAM. A board that comes up
  in the system bootloader looks like firmware that hangs immediately.
  On STM32F7 the boot addresses are option bytes, not just a pin — read them:

  ```
  x/3wx 0x40023C14   # FLASH_OPTCR, OPTCR1, OPTCR2
  ```

  `OPTCR1` holds `BOOT_ADD0` in bits 15:0 and `BOOT_ADD1` in 31:16, each a
  **boot address shifted right by 14**. `(measured on F777)` `OPTCR1 =
  0x00400080` → `BOOT_ADD0 = 0x0080`, i.e. `0x0080 << 14 = 0x00200000`, and
  `BOOT_ADD1 = 0x0040` → `0x00100000`. So this part boots from the **ITCM alias
  of flash**, which is why its vector table is not at `0x08000000`.

  The two are selected by the BOOT0 pin: `BOOT_ADD0` when it is low,
  `BOOT_ADD1` when high. Identify what each points at by **reading it and
  comparing**, not by decoding the number — `(measured on F777)` `0x00100000`
  returns the same words as `0x1FF00000`, with vectors into `0x1FF0xxxx`, which
  identifies it as the ITCM alias of the ST factory system bootloader and makes
  BOOT0-high the DFU route.

  `OPTCR` bits 15:8 are `RDP`. `0xAA` is level 0, no readout protection, and is
  why the debugger can read flash at all. A target that suddenly refuses memory
  reads has had RDP raised, which no debug session recovers from without a mass
  erase.
- **`SCB->VTOR`** relocates the vector table, and **it moves while the system
  runs**. Read it at the moment you need it rather than once. `(measured on
  F777)` the same board read `VTOR = 0x00200000` under one image and
  `0x08040000` under another, so neither is a property of the board. On
  Cortex-M7 the same flash is reachable through two buses — `0x08000000` via
  AXIM and `0x00200000` via ITCM — so a `VTOR` that looks wrong may just be the
  other alias of the address the linker script names, differing by `0x07E00000`
  while pointing at one word of flash. Do not "correct" it to `0x08000000`; see
  `meadow.md` for the worked case.
- **The RAM-copy trap from `../SKILL.md` applies here.** Startup code copies
  `.data` from flash to RAM before `main`, and any function placed in RAM
  (`.RamFunc`, ITCM) is copied the same way. A software breakpoint set in that
  region before the copy is overwritten by it while GDB still reports it armed.
  Use the two-stage arming pattern: a hardware breakpoint in flash after the
  copy, then set the RAM breakpoints.
- `monitor reset halt` and `monitor reset init` differ — `init` runs the cfg's
  init hooks, which on some families set up clocks or flash latency. If halting
  after reset produces a target that cannot read flash, try the other one.
- Triggers do not always clear when a GDB session is killed. A board that hits a
  breakpoint nobody set is usually a stale trigger from a dead session; connect
  and clear, or power-cycle.

## Mode and privilege `(assumed)`

| Register | Reports |
|---|---|
| `IPSR` (low bits of `xPSR`) | exception number currently executing; 0 = thread mode |
| `CONTROL` bit 0 (`nPRIV`) | thread mode unprivileged when set |
| `CONTROL` bit 1 (`SPSEL`) | thread mode on PSP when set |
| `EXC_RETURN` (in `LR` on handler entry) | which stack the interrupted context used, and whether it was thread or handler mode |

**The template's warning bites here.** Halted inside a handler, `IPSR` gives you
the handler's exception number, not the mode of the code it interrupted. The
interrupted context is recovered from the exception stack frame, whose location
`EXC_RETURN` tells you: bit 2 set means the frame is on PSP, clear means MSP.
Read `LR` at handler entry before anything overwrites it — after a few calls it
is gone and the frame can no longer be found reliably.

## Interrupt controller `(assumed)`

The NVIC. To identify what is live, prefer reading state over consulting a
table:

```
# SCB->ICSR: VECTACTIVE in the low 9 bits is the exception currently executing,
# VECTPENDING at bits 12-20 is the highest-priority one waiting
x/1wx 0xE000ED04
# NVIC->IABR[0..]: one bit per external interrupt, set while it is active
x/8wx 0xE000E300
```

**The off-by-16.** CMSIS `IRQn_Type` numbers external interrupts from 0, and
system exceptions negatively. `IPSR` and `VECTACTIVE` number every exception
from 0, with external interrupt *n* appearing as *n + 16*. Reading a vendor IRQ
number straight out of `IPSR` is the classic way to blame the wrong peripheral.

**Priority grouping.** `AIRCR` bits 10:8 (`PRIGROUP`) split the priority field
between preemption and subpriority. `(measured on F777)` `AIRCR = 0xFA050000`
→ `PRIGROUP = 0`, so with 4 implemented bits every bit is preemption priority
and there is no subpriority. Two interrupts at the same level therefore cannot
preempt one another at all, which changes what a "higher priority" fix does.

**Priority masking, and where the expensive bugs live.** `PRIMASK`, `FAULTMASK`
and `BASEPRI` all mask, and `BASEPRI` is the one an RTOS uses. Two things vary
and must be checked on the part:

- **How many priority bits the part implements** (`__NVIC_PRIO_BITS`,
  commonly 4 on STM32, giving 16 levels in the *top* bits of an 8-bit field).
  Priorities are left-aligned, so a raw register value is not the level.
- **Whether the firmware's RTOS ceiling matches.** With FreeRTOS,
  `configMAX_SYSCALL_INTERRUPT_PRIORITY` must be expressed in the same
  left-aligned form, and any interrupt that calls a `FromISR` API must be at or
  below it numerically-higher-is-lower-priority. Getting this wrong produces
  corruption that appears far from its cause. The `freertos` skill covers the
  `configASSERT` that catches it; enable it before hunting anything else. NuttX
  has the equivalent ceiling and no such assertion — see `meadow.md`.

## Failure signatures `(assumed)`

**Check what is even enabled first** `(measured on F777)`. Two registers decide
whether a fault reaches a handler at all:

```
x/1wx 0xE000ED24   # SHCSR - MEMFAULTENA b16, BUSFAULTENA b17, USGFAULTENA b18
x/1wx 0xE000ED14   # CCR   - DIV_0_TRP b4, UNALIGN_TRP b3
```

Measured on the Meadow F777 running NuttX: `SHCSR = 0x00010000` — only
**MemManage is enabled**, so BusFault and UsageFault are not taken as
themselves and **escalate straight to HardFault**. A search for a BusFault
handler that never runs is wasted; the handler that runs is HardFault, and
`HFSR.FORCED` is set to say so.

`CCR = 0x00070200` on the same board: `DIV_0_TRP` and `UNALIGN_TRP` are both
**clear**, so divide-by-zero and unaligned access do not fault — they return
nonsense silently. Set them before hunting a bug of either kind. NuttX ships a
GDB macro for exactly this; see `meadow.md`.

**Cortex-M7 caches change what the debug link sees** `(measured on F777)`. The
same `CCR` has bits 16, 17 and 18 set — data cache, instruction cache and
branch prediction all on, matching `CONFIG_ARMV7M_DCACHE`/`ICACHE` in the
build. A value written through the debug link may not be seen by the core, and
memory read through the link may be stale with respect to the core's cache.
This board configures `CONFIG_ARMV7M_DCACHE_WRITETHROUGH=y`, which makes CPU
writes visible to a debug read; nothing makes debug writes visible to the CPU.
Suspect the cache before concluding that a poke had no effect.

On a fault, read the status registers before doing anything that might clear
them:

```
x/1wx 0xE000ED28   # CFSR  - MemManage, BusFault, UsageFault sub-registers
x/1wx 0xE000ED2C   # HFSR  - FORCED bit means the real cause is in CFSR
x/1wx 0xE000ED34   # MMFAR - faulting address, valid only if CFSR.MMARVALID
x/1wx 0xE000ED38   # BFAR  - faulting address, valid only if CFSR.BFARVALID
```

`HFSR.FORCED` set means a configurable fault escalated because its handler was
disabled or it faulted again — the useful information is in `CFSR`, not `HFSR`.

Two things to establish for the board, because both are firmware decisions and
neither is knowable from the architecture:

- **What an assertion does** — reboot or hang — and where it prints.
- **What the fault handler does.** A default handler is often `while (1) {}`,
  which is indistinguishable from the hang being chased and produces no output
  at all. If that is what is there, replace it before investigating, or rely on
  halting and reading `CFSR` rather than on anything reaching the console.

**Lockup.** A fault taken inside the fault handler puts the core in lockup,
where it stops fetching and the debug link may still attach. A target that
halts at an address of `0xFFFFFFFE` is locked up, not executing.

## Wrong turns

Empty on purpose. This is the section the template calls the most valuable part
of the file and the only part that cannot be reconstructed later. Fill it from
real sessions — each entry being what was believed, what it cost, and what
would have caught it sooner — and delete this paragraph once there is one.
