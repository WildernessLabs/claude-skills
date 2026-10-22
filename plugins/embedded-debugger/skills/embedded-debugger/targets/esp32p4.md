# Target profile: ESP32-P4 (Espressif, RISC-V, CLIC)

**Read `esp32-family.md` first** - toolchain, flashing, breakpoint types, the
bootloader erasing RAM breakpoints, trigger leakage, the two console ports and
the interrupt-numbering trap are all there and all apply here. This file is
only what is specific to the P4 and to its CLIC.

Derived from a NuttX `BUILD_PROTECTED` bring-up on an M5Stack Tab5, 2026-08.

## Board and connection

```bash
openocd -c 'set ESP_RTOS none' -f board/esp32p4-builtin.cfg   # NuttX; see below
riscv32-esp-elf-gdb -q -batch -x s.gdb nuttx
```

`set ESP_RTOS none` here is because this work runs **NuttX**, not because it is
right for the P4. On an ESP-IDF target leave the default - see the `ESP_RTOS`
table in `esp32-family.md`.

- Internal SRAM (`.iram0.text`) is at `0x4ff00000`+; XIP flash at `0x40000000`+.
- Dual-core: NuttX runs on `cpu0` while `cpu1` sits in ROM around `0x4fc0283e`.
- The M5Stack Tab5's part reports **revision v1.0**, so it takes the pre-rev3
  paths below. Console is UART0 on `/dev/cu.usbserial-*`; the USB-C port is
  USB-Serial/JTAG on `/dev/cu.usbmodem*`.

## Reported PC is post-instruction

Breakpoint stops report the PC **after** the trapped instruction:
`hbreak *0x4021819e` reports `0x402181a0`, `break *0x4ff0016a` reports
`0x4ff0016e`. Accept both `ADDR` and `ADDR + size` when guarding on `$pc`.

## Privilege (`$priv`) is not trustworthy where you want it

`$priv` reports `0` = U, `1` = S, `3` = M - but it reads `3` whenever the target
is halted **inside a trap handler**, regardless of the interrupted task's own
privilege. It is only meaningful at plain user-code addresses.

Worse, RISC-V `dcsr.prv` holds the privilege the core will *resume into*, so
**a privilege reading taken across a halt-and-resume is not evidence** - halting
user code and resuming it can change privilege. That manufactured a phantom
register corruption that a code change was then written against. Use
observe-only halts that are never resumed, or read `mcause` instead.

Validate it before relying on it: break on an `ecall` site whose trap reports
`mcause = 8`, confirm `$priv` reads `0` there, and cross-check with `mcause`
after a `stepi`.

## Do not single-step the exception return path

Software and hardware breakpoints both wedge this target after ~3 stops on the
trap-return path, parking on the same instruction each time, at 30-48 s per
stop. Instrument the kernel instead - a ring buffer read out of RAM at an
observe-only halt costs one halt for thousands of events.

## CLIC interrupt controller

The P4 is a **CLIC** part. Per the vendored HAL's `soc_caps.h`, so are the C5,
C61, H4 and S31; the C6 and H2 are PLIC, and the C3, C2 and the Xtensa parts
are neither. Nothing below applies to a non-CLIC ESP32.

`mcause` carries the wide CLIC layout:

| Bits | Field |
|---|---|
| 31 | interrupt (vs exception) |
| 30 | `minhv` |
| 29:28 | previous privilege (`mpp`) |
| 27 | `mpie` |
| 23:16 | previous interrupt level (`mpil`) |
| 11:0 | exception/interrupt code |

- `mcause[29:28]` **is** correctly aliased to `mstatus.MPP`, write-through in
  both directions (verified by writing each and observing the other follow).
- `mie`/`mip` read zero and are unused; enables live in the CLIC's own
  registers.
- Exceptions are non-vectored: `_vector_table` is a bare `j exception_common`.
- `mcause` is **architectural return state on a CLIC part, not a diagnostic**.
  `mret` consults `mpp`, `mpie` and `mpil`. An RTOS that saves and restores
  `mstatus` but not `mcause` will fail in ways that look impossible.

## Pre-rev3 CLIC is non-standard in three ways

The Tab5's v1.0 silicon takes `CONFIG_ESP32P4_SELECTS_REV_LESS_V3`:

1. `mintstatus` is at CSR **`0x346`**, not the ratified `0xFB1`.
2. The interrupt **threshold is a memory-mapped register**
   (`CLIC_INT_THRESH_REG` = `0x20800008`), not a CSR.
3. `NLBITS` = 3, so a level L encodes as `(L << 5) | 31`: level 0 is **`0x1f`**,
   level 1 is `0x3f`, level 2 is `0x5f`. Two traps here. A `mil` or `mpil` of 63
   is *level 1*, not an out-of-range value - do not read it as corruption. And
   **`0x00` is not level 0**, it is a non-canonical encoding; code that "clears
   `mpil`" is not setting level 0, which invalidated three separate fix
   attempts before anyone noticed.

**The CLIC register block reads back as zero over the JTAG debug bus**
(`0x20800000` config/info/threshold, `0x20801000` + n*4 per-source
IP/IE/ATTR/CTL). It must be sampled by code running on the target. A GDB read
returning all zeros there means "not readable", not "not configured".

GDB will not name `mintstatus` under any spelling - neither `$mintstatus` nor
`$csr838` works. Read it through a target-side global, sampled from **task**
context: a value sampled inside an interrupt handler cannot answer "was the
level ever unwound", because it is legitimately raised there.

**`mintstatus` is READ-ONLY** (measured: a write is silently ignored and the
read-back is unchanged).  `mil` is raised by trap entry and lowered *only* by
`mret` restoring it from `mcause.mpil`.  The consequence is worth internalising:
if `mil` is left at a level that masks the sources you need, and the code
running takes no trap, **the state is unrecoverable by software** - the level
blocks every trap that could lower the level.  A spinning idle loop in that
state is a permanent deadlock, and `WFI` returns immediately because an
interrupt is pending-but-masked.

Delivery rule: an interrupt is taken only if its level **exceeds** `mil` and
exceeds the threshold.  Level 1 does not exceed level 1.

## External PSRAM keeps state across a reset

The Tab5 carries 32 MB of external PSRAM at `0x48000000`-`0x4a000000`
(`SOC_EXTRAM_LOW`/`HIGH` describe a wider `0x48000000`-`0x4c000000` window).

**A failed PSRAM init poisons the part until power is removed.** The mode
registers live on the external die, and `EN` resets the ESP32-P4 but not that
separate chip, so whatever half-configured state a failed init left behind
survives every reset esptool, OpenOCD or the RTOS can issue. Every subsequent
boot then fails *identically*.

This is vicious for two reasons:

1. **It makes a known-good image look broken.** Re-flashing a build that
   demonstrably worked an hour ago reproduces the failure exactly, which reads
   as "the build is broken" rather than "the board is poisoned".
2. **It silently invalidates every experiment run afterwards.** Two hypotheses
   were tested and wrongly rejected on a poisoned board before anyone noticed
   the board, not the change, was the constant.

So: after any boot failure involving PSRAM, **power-cycle before drawing any
conclusion**, and treat results gathered since the first failure as void.
Removing power is the only fix; ask the user for it, it is not something the
debugger can do.

**It bites builds with PSRAM disabled too.** `mmu_hal_unmap_all()`
(`components/hal/mmu_hal.c`) unmaps the flash *and* PSRAM MMUs
unconditionally - it is not guarded by `CONFIG_SPIRAM` - so a store to the
PSRAM MSPI register faults regardless of whether the config uses PSRAM. The
failure therefore looks unrelated to PSRAM, which is what makes it hard to
place. Signature: a store access fault (`mcause` exccode 7) with `mtval` in the
`0x5008xxxx` peripheral window, from `mmu_ll_unmap_all` inlined into
`mmu_hal_unmap_all` <- `mmu_hal_init` <- `bootloader_init_ext_mem` <-
`bootloader_init`.

## The USB-Serial/JTAG console re-enumerates mid-boot

When NuttX takes the USB-Serial/JTAG peripheral, the device re-enumerates: the
`/dev/cu.usbmodem*` minor number changes and any handle opened across the reset
dies with `[Errno 6] Device not configured`, part-way through the boot log.

It matters only where that port *is* the console - on this board that is the
flat configs (`CONFIG_ESPRESSIF_USBSERIAL`). The protected `knsh` configs put
the console on the UART adapter (`/dev/cu.usbserial-*`), which never
re-enumerates, so their whole boot can be captured in one open.

Practical consequence: on a flat config you cannot capture NuttX's own boot
banner over its own console. Reset, wait ~3 s for re-enumeration, then open and
press Enter for a prompt. If you need the banner, build a config whose console
is the UART.

## ramtest destroys whatever it is pointed at

`testing/mm/ramtest -a <addr>` writes marching ones, marching zeroes and three
patterns over the address range given. Pointed at the base of a heap-backed
region it overwrites the allocator's region header, and the next `free` faults
on a corrupted free-list node - which looks like a memory bug in whatever was
under test.

It is sound as an *access-permission* probe (does user mode fault touching this
window?), which is what it is useful for when checking a PMP grant. Reset the
board afterwards. Given no `-a`, it `malloc()`s its own buffer and frees it,
which is non-destructive and usually what you want.

Note the address is parsed with `strtoul(..., 0)`, so it needs a `0x` prefix
despite the usage text saying "hex-address"; a bare `48000000` is read as
decimal and probes a bogus address.

## USB-Serial/JTAG registers

`DR_REG_USB_SERIAL_JTAG_BASE = 0x500d2000`:

| Address | Register |
|---|---|
| `0x500d2008` | `INT_RAW` |
| `0x500d200c` | `INT_ST` |
| `0x500d2010` | `INT_ENA` |

Bits: `SOF = 1<<1`, `SERIAL_OUT_RECV_PKT = 1<<2`, `SERIAL_IN_EMPTY = 1<<3`,
`BUS_RESET = 1<<9`.

## PMP

16 entries, **128-byte granularity**, and **no Smepmp** (`mseccfg` traps on
read), so lock bits are irreversible and must be prevented rather than undone.

**"The first free entry" is usually the wrong entry.**  PMP is lowest-matching-
entry-wins, so a grant placed above an entry that already denies the address is
never consulted.  A NuttX protected build occupies 0-7 as TOR pairs alternating
deny/allow with kernel IRAM inside a deny range; `riscv_next_free_pmp_region()`
returns 8 and the grant does nothing.  Borrow a lower, unlocked entry and put it
back.

The failure mode is a **fetch fault, not a visible error**: `mcause` exccode 1
with `mtval` = the address.  If your harness only attaches ECALLU/ECALLM it will
not catch it, so it reaches the board's fault handler and spins - looking
exactly like the hang you were hunting.  Halt and read `mcause` before believing
a hang is your hang.

Under ESP-IDF nothing is needed: `cpu_region_protect.c` grants U-mode R+X on
IRAM text via entry 4 (`PMP_TOR | RX`, and its `RX` includes `PMP_L`, so it
applies to U-mode).  It sets 17 entries and locks all of them but uses only
**1-3, 10-13 and 27-31** - **14-26 are free**.

## Wrong turns specific to this part

Beyond the family list:

1. **`$priv` used before validation.** It read `3` everywhere sampled, and no
   check was made that it could read anything else.
2. **Attributing a hang to the mechanism nearest to hand.** A trap that latched
   `mepc`/`mcause`/`mstatus` and never reached `mtvec.base` was blamed on the
   private vector table being written; it was actually the `mcause` value the
   preceding `mret` consumed, and the same failure occurred on the stock
   vectors. The control that would have caught it was one flag away.
3. **Reversing a correct conclusion on one unreplicated sample.** A "healthy"
   reading used to refute a theory turned out to have come from the failing
   build. Take the comparison sample from the *known-good* run explicitly.
4. **Treating the board as a constant while it was the variable.** After a
   PSRAM init failure the external die stays poisoned across every reset, so a
   known-good image reproduced the fault and two hypotheses were tested and
   wrongly rejected before the board itself was suspected. When a control that
   *should* pass starts failing, doubt the hardware state before the change.
   See "External PSRAM keeps state across a reset".
