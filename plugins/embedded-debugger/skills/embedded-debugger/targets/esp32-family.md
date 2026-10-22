# Target family: Espressif ESP32

Applies across the ESP32 line - Xtensa (ESP32, S2, S3) and RISC-V (C3, C6, H2,
P4 and later). Chip-specific detail lives beside this file; see `esp32p4.md`.

Provenance: derived from a NuttX `BUILD_PROTECTED` bring-up on an ESP32-P4
(M5Stack Tab5), 2026-08. So **everything here was measured on one chip running
one RTOS**, and two kinds of over-generalisation are easy to make from it:

- *across chips* - marked `(measured)` where it was observed here and
  `(assumed)` where it is expected to hold but was not exercised;
- *across RTOSes* - sections headed **(NuttX)** describe that RTOS's drivers and
  Kconfig, not the silicon. Under ESP-IDF the equivalent code is different and
  the conclusion may invert. The `ESP_RTOS` table below is the worked example of
  exactly that.

Read alongside `../SKILL.md`.

## Environment

```bash
source ~/.espressif/tools/activate_idf_v5.5.4.sh   # provides gdb + openocd
openocd -f board/<chip>-builtin.cfg                # see ESP_RTOS below
<arch>-esp-elf-gdb -q -batch -x s.gdb <elf>
```

- Connect with `localhost:3333`. The bare `:3333` form fails to resolve on
  macOS.
- Match the toolchain to the tree. A newer IDF release will often fail to build
  an older one; pin the version rather than taking whatever is on `PATH`.

### `ESP_RTOS`: which RTOS is actually running?

Espressif's OpenOCD defaults to **FreeRTOS thread awareness**. Whether that
helps or hurts depends entirely on what is on the target, so decide before the
first halt rather than copying a command line:

| Firmware | Setting | Why |
|---|---|---|
| ESP-IDF | leave the default | IDF *is* FreeRTOS, so the awareness matches reality and gives you real thread listings. `(measured, ESP32 + IDF v5.5.3, 2026-09 - see esp32-pico-d4-espprog.md)`: `info threads` gave 12 real tasks with names, states and per-core placement |
| NuttX | `-c 'set ESP_RTOS none'` | `(measured)` without it, OpenOCD errors on every halt - `Don't have the number of threads in FreeRTOS!` - which is noise plus a per-halt cost |
| Zephyr, bare metal, anything else | almost certainly `none` | the server is looking for structures that are not there |

The general rule is section 5 item 7 of `../SKILL.md`: disable RTOS awareness
only when the RTOS is not the one the server expects. `none` is not a
free win to apply by reflex - on an IDF target it discards working thread
awareness.

## Flashing and the debug link fight over the USB device

**Kill OpenOCD before flashing.** esptool takes the USB device, and the stale
OpenOCD session then fails every DMI transaction (`dmi_scan failed jtag scan`,
`Failed DMI write`, `Unable to execute JTAG queue`) in a way that reads as a
dead target. Restart OpenOCD after flashing.

## Memory map and breakpoint types

| Region | Breakpoint |
|---|---|
| Internal SRAM (`.iram0.text`) | `break` (software) |
| Flash, XIP (`.flash.text`) | **`hbreak` only** |
| A second image (protected/kernel builds) | **`hbreak` only** |

A software breakpoint in XIP flash silently does nothing.

## The ROM bootloader erases RAM breakpoints

The ROM bootloader copies RAM segments from flash on **every** boot. The boot
log names them:

```
load:0x4ff00000,len:0x54f4      <- .iram0.text
load:0x4ff05500,len:0x1b24
```

A software breakpoint inside those ranges, set before `monitor reset halt`, is
overwritten before the code runs - while GDB still reports it armed. Use the
two-stage pattern from `../SKILL.md`: `hbreak` a flash address first,
`continue`, and only then set RAM breakpoints.

`monitor reset halt` also issues a CPU reset that clears hardware triggers, so
those must likewise be set after it, not before.

## Hardware triggers leak

Killing a GDB session leaves its triggers armed in silicon, and
`monitor reset halt` does not clear them. The next session fails with:

```
Cannot insert hardware breakpoint N: Remote failure reply: 0E.
```

The only reliable fix is **restarting OpenOCD**. Run long jobs in the
background so a tool timeout cannot kill them; a foreground run cut at two
minutes burns a trigger each time.

On multi-core parts, triggers are per-hart and the second core usually sits in
ROM - check `info threads` before concluding a trigger did not fire.

## Console: two ports, and they are not interchangeable

Parts with a built-in USB-Serial/JTAG (S3, C3, C6, H2, P4 and later) present a
different device from a UART routed through an external bridge:

| Port (macOS) | What it is | Used for |
|---|---|---|
| `/dev/cu.usbmodem*` | built-in USB-Serial/JTAG | esptool, OpenOCD |
| `/dev/cu.usbserial-*` | UART via an external bridge | the RTOS console, if configured there |

Because they are separate devices, you can **hold the console port open while
resetting through the other one** - which is what makes non-interactive capture
work (`../SKILL.md` section 1b):

```bash
esptool.py --port /dev/cu.usbmodem101 --after hard_reset --no-stub chip_id
```

**Board-dependent: that reset can leave the chip in download mode.** esptool's
`--before default_reset` on the built-in USB-Serial/JTAG is precisely how it
forces download mode, and on some parts `--after hard_reset` does not bring
them back out - every reset then lands at `boot:0x4 (DOWNLOAD(...))` /
`waiting for download`, which reads exactly like an image that will not boot.
It works on the P4; it does **not** on the C6 (see `esp32c6.md` for the DTR/RTS
sequence that gives a normal boot). Check the ROM banner's `boot:` field once
before trusting the recipe on a new board - `boot:0x4` means you reset it into
download mode, not that the flash is bad. `(measured, C6)`

Also note the port-naming heuristic above is a convention, not a rule: on some
boards the external bridge enumerates as `usbmodem*` too, and both interfaces
report the **same MAC**. Identify them by behaviour - which one carries the
console, which one esptool can write through - not by device name.
`(measured, C6)`

The USB CDC path also transmits roughly **one byte per USB frame**, so a
breakpoint on any exception-return path fires once per character - a 32-byte
banner is ~32 stops. Repeated identical frames in such a sweep are normal TX
drain, **not** an interrupt storm; check the driver's transmit buffer tail is
advancing before concluding otherwise. `(measured, P4)`

### Prefer a real UART for interrupt or privilege work (NuttX)

NuttX's `esp_usbserial_write()` busy-waits on the TX FIFO with no timeout and
with interrupts disabled, so any host that stops draining the CDC endpoint
wedges the kernel. That has caused several wrong diagnoses. Two consequences:

- **`CONFIG_DEBUG_IRQ_WARN` is dangerous** on the USB console - it makes the
  interrupt demux print from inside an ISR through that blocking path.
- `CONFIG_DEBUG_SYSCALL_INFO` and `CONFIG_DEBUG_SCHED_INFO` flood the console
  and change timing enough to mask the bug you are chasing.

This is a property of that driver, not of the silicon - ESP-IDF's USB serial
JTAG driver is a different implementation and has not been checked.

## The hardware stack guard fires on any stack switch the RTOS did not do (ESP-IDF)

`CONFIG_ESP_SYSTEM_HW_STACK_GUARD` (on by default) arms the assist_debug SP
monitor on the bounds of the **current FreeRTOS task**. Any code that puts a
different stack in `sp` - a U-mode or coroutine window, a private ISR stack -
latches a violation on the `sp` write itself. `(measured, P4, IDF v5.5.4)`

The panic it produces points away from the cause in three separate ways, so it
is worth recognising by shape:

- `mcause` reads **`0x1b`** - that is `ETS_ASSIST_DEBUG_INUM`, an interrupt
  number IDF stuffs into the frame, not an architectural exception code.
- If the switch happened with interrupts masked, delivery waits for the unmask,
  so `MEPC` names the **unmask site** and the printed `Stack pointer` is
  whatever `sp` holds by then - typically back in bounds and looking healthy
  next to the bounds printed beside it.
- The only field naming the guilty instruction is
  `Detected in task "<name>" at 0x<pc>`, which is the PC assist_debug latched.

Stopping the monitor from C around the switch does **not** hold: FreeRTOS's
`rtos_int_exit` in `portasm.S` re-arms it and re-points the bounds at the
current task on every interrupt exit, so any tick landing before the switch
undoes the stop. The stop has to live inside the interrupt-masked region that
does the switch, i.e. in assembly. IDF ships the macros for it in
`esp_private/hw_stack_guard.h` (`ESP_HW_STACK_GUARD_MONITOR_STOP_CUR_CORE
reg1 reg2` and `..._START_CUR_CORE`); the header is assembler-clean and, via a
`PUBLIC` leak in `esp_system/port/CMakeLists.txt`, needs no CMake change to
include from an app component. Note that `SOC_CPU_CORES_NUM` is undefined in
assembly, so `_CUR_CORE` silently resolves to the **core-0** registers - correct
only for code pinned to core 0, and true of IDF's own `portasm.S` too.

### Re-pointing the bounds at another stack, rather than standing the guard down

`esp_private/hw_stack_guard.h` also supplies `..._SET_BOUNDS_CUR_CORE reg1`
(bounds in `a0`/`a1`), so the monitor can be made to follow a private stack for
the duration. Three further traps, all `(measured, P4)`:

- The trap vector must not put a kernel address in `sp` - the classic
  `csrrw sp, mscratch, sp` entry - while the new bounds are live, or **every**
  trap latches a violation. Swap the context pointer through `t0` instead and
  leave `sp` alone.
- There is **no separate interrupt enable**: `ASSIST_DEBUG_CORE_n_MONITOR_REG`
  is `#define`d to `ASSIST_DEBUG_CORE_n_INTR_ENA_REG`, the same bits. You
  cannot keep the monitor latching while gating delivery, and re-enabling
  "the interrupt" re-enables the monitor - which then fires on the very
  `SET_BOUNDS` store that moves the bound past the current `sp`.
- `ETS_ASSIST_DEBUG_INTR_SOURCE` is **one** matrix source and
  `esp_hw_stack_guard_init()` runs on every core, so a core-0 violation is
  delivered to core 1 too, where a core-0 interrupt mask means nothing. Core 1
  wins that race every time and panics with `ASSIST_DEBUG is not triggered BUT
  interrupt occurred!`. The only lever is
  `esp_rom_route_intr_matrix(1, ETS_ASSIST_DEBUG_INTR_SOURCE, ETS_INVALID_INUM)`,
  which costs core 1 its own guard reporting.

`INTR_RAW` is sticky, so polling it on the way out catches even an excursion
that was put back before the trap. The upper bound is inclusive: `sp` equal to
`SP_MAX` does not fire.

## Interrupt source numbering: the SDK and the RTOS disagree (NuttX)

**The ESP HAL and NuttX maintain different `*_INTR_SOURCE` numberings.**

- HAL: `components/soc/<chip>/include/soc/interrupts.h`
- NuttX: `arch/risc-v/include/<chip>/irq.h`

On the P4, source 22 is `ETS_SYS_ICM_INTR_SOURCE` in the HAL table and
`USB_SERIAL_JTAG_INTR_SOURCE` in NuttX's. Reading the wrong one misidentified
the console interrupt as an interconnect fault and cost several hours.

Identify a live interrupt at runtime instead of from a table:

```gdb
break *<isr_adapter_func>
continue
p *(struct intr_adapter_from_nuttx *)$a0
# -> {func = <esp_interrupt>, irq = 39, arg = <g_uart_usbserial>}
```

All peripheral IRQs funnel through **one demux slot**, so `g_irqvector[irq]` is
not the handler lookup - it is `g_irqvector[IRQ_TO_NDX(irq)]`, and with
`CONFIG_ARCH_MINIMAL_VECTORTABLE` the table is sized by
`CONFIG_ARCH_NUSER_INTERRUPTS`, not `NR_IRQS`. A short table is by design, not
corruption.

Because everything folds onto that slot, **attaching a handler to an IRQ number
at or above `ESP_IRQ_FIRSTPERIPH` overwrites the demultiplexer** and every
device interrupt afterwards panics with a nonsense "unknown exception". Calling
`riscv_exception_attach()` after boot does exactly this; at boot it is harmless
only because the demux handler is attached afterwards.

## Asserts hang, they do not reboot (NuttX)

With `CONFIG_BOARD_RESET_ON_ASSERT=0`, `reset_board()` is an infinite
`for(;;) { up_mdelay(250); }`. A halt landing in `udelay_coarse` / `up_mdelay` /
`reset_board` means an assertion **already fired**; GDB still reports the target
as "running".

Similarly, a board-local fault handler that spins is indistinguishable from the
hang you are chasing unless it prints somewhere you are actually watching -
`esp_rom_printf()` does **not** reach a UART0 console.

## Wrong turns, all avoidable by measuring first

1. **Trusting silent breakpoints.** Conclusions drawn from breakpoints that
   never fired - some erased by the bootloader, some set after the event they
   watched for, one never armed at all.
2. **Registers in breakpoint conditions.** A register read in a GDB condition
   is frame-relative and gave a different value than the same register at the
   stop, producing three false negatives.
3. **Reading the wrong interrupt table**, yielding a confident and completely
   wrong peripheral identification.
4. **Patching an inferred bug.** Three source edits for a livelock that did not
   exist; one appeared to introduce a new fault, and the attribution of *that*
   was also wrong.
5. **Reading a stale serial buffer** as fresh output across runs.
6. **Reporting a test's results through `printf()`** on the interrupt-driven
   console. The console is what dies in the failure being chased, so a wedged
   core looked exactly like a console fault and a whole session went into
   "fixing" the console. Report through the polled low-level path and print a
   BEGIN line per stage as well as a result line.
7. **Believing a fault would announce itself.** A user-mode PMP fault reported
   through `esp_rom_printf()` never reached the console and presented as a
   silent hang - and was mistaken for the bug under investigation.
