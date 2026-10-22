# Target profile: ESP32-PICO-D4 (Espressif, Xtensa LX6 dual-core) via ESP-Prog

Read `esp32-family.md` first - this file records only what is specific to this
board and probe, plus the things the family file got wrong or did not cover for
classic ESP32.

Provenance: measured 2026-09-14 on the Meadow-ESP32 `test/` build (ESP-IDF
v5.5.3, branch `Fix/Issue387`, app `2.5.5.0-120-g4ae55178`), macOS 25.6.
OpenOCD v0.12.0-esp32-20251215, xtensa-esp32-elf-gdb 16.3.

---

## Connection `(measured)`

ESP-Prog: a single FTDI **FT2232H** ("Dual RS232-HS", VID `0x0403`
PID `0x6010`). Channel A is JTAG, channel B is the UART. macOS enumerates
**both** channels as serial devices:

| Device | Channel | Role |
|---|---|---|
| `/dev/cu.usbserial-100` | A | JTAG - claimed by OpenOCD over libusb |
| `/dev/cu.usbserial-101` | B | **application console**, 115200 8N1 |

The higher-numbered device is the console. Confirm by behaviour, not by name -
the numbers are location-derived and change between hosts.

Note that OpenOCD claims channel A over libusb **while macOS still exposes it
as `/dev/cu.usbserial-100`**. No driver unload or `kextunload` was needed.

```bash
cd <repo root>          # the repo's own cfgs live here
openocd -f interface/ftdi/esp_ftdi.cfg -f board/esp-wroom-32.cfg
```

`board/esp-wroom-32.cfg` prints a deprecation warning and works. Both TAPs are
found (`esp32.tap0`/`tap1`, IDCODE `0x120034e5`) and both cores examined.

GDB: `~/.espressif/tools/xtensa-esp-elf-gdb/16.3_20250913/xtensa-esp-elf-gdb/bin/xtensa-esp32-elf-gdb`.
Connect to `localhost:3333`. Run with `-nx` - the repo ships a `test/gdbinit`
(no leading dot, so not auto-loaded) that arms sysview tracing breakpoints.

**RTOS awareness: leave the default on.** This is ESP-IDF, so OpenOCD's
FreeRTOS awareness matches reality - `info threads` gives real task names,
states and per-core placement (`State: Running @CPU0` / `@CPU1`).
`(measured)` - confirms the family file's `(assumed)` row for ESP-IDF.

## Console `(measured)`

- `/dev/cu.usbserial-101`, 115200, `CONFIG_ESP_CONSOLE_UART_NUM=0`, on
  GPIO 3/1 (the boot log states this).
- **Separate device from the debug link**, so the port can be held open across
  a reset driven through JTAG. That is what makes non-interactive capture work.
- Non-interactive reset without touching esptool (and without fighting OpenOCD
  for the USB device): drive OpenOCD's **telnet port 4444** from the same
  script that holds the serial port open:

  ```python
  ser = serial.Serial("/dev/cu.usbserial-101", 115200, timeout=0.2)
  time.sleep(0.3); ser.reset_input_buffer()
  s = socket.create_connection(("127.0.0.1", 4444)); s.recv(65536)
  s.sendall(b"reset run\n")
  ```

  This gives a clean `rst:0x3 (SW_RESET),boot:0x12 (SPI_FAST_FLASH_BOOT)` -
  **not** the download-mode trap the family file records for USB-Serial/JTAG
  parts. Classic ESP32 has no built-in USB-JTAG, so that trap does not apply.
- The console is bidirectional and usable as an instrument: writing `\r\n`
  drives the Unity interactive test menu and the listing comes straight back.
- Polled output path: `esp_rom_printf()`. Per the family file it does **not**
  reach a UART0 console on some setups - **not verified here**, so check it
  before relying on it for interrupt/fault reporting. `(assumed)`

## Memory map and breakpoints `(measured)`

| Region | Breakpoint |
|---|---|
| `.flash.text` (XIP, ~`0x400d0000`+; `app_main` at `0x400ea55c`) | `hbreak` |
| IRAM (`0x40080000`-`0x400a0000`) | `break` |
| DROM read-only data | n/a - `0x3F400000`+ |

`esp_app_desc` sits at **`0x3F400020`**, not `0x3F400000`.

Hardware triggers: **at least 3** `hbreak`s inserted and reported armed
simultaneously on cpu0, contradicting the usual "ESP32 has 2 IBREAK" figure.
Not pushed past 3, and breakpoint/watchpoint pool sharing not probed.
`(measured, partial)` - `gdbinit` in the repo sets
`remote hardware-watchpoint-limit 2`.

## Boot and reset `(measured)`

- ROM bootloader copies RAM segments on every boot; the boot log names them
  (`load:0x3fff0030,len:296` / `load:0x40078000,len:13232` /
  `load:0x40080400,len:2892`, `entry 0x40080554`). The two-stage arming pattern
  from `../SKILL.md` applies - `hbreak app_main`, `continue`, then RAM
  breakpoints.
- `monitor reset halt` leaves cpu0 at `pc=0x40000400` in ROM with reset cause 3
  (software core reset); cpu1 reports cause 14 (reset by cpu0).
- No leaked-trigger failures were seen across six GDB sessions against one
  long-lived OpenOCD, including one session GDB aborted mid-script.

## Mode and privilege

**Unanswered.** No privilege work done on this target. Xtensa LX6 has no
U-mode split of the kind the P4/RISC-V notes describe, so most of that
material does not transfer.

## Interrupt controller

**Unanswered.** No interrupt work done here. The family file's NuttX
interrupt-numbering warning is about a different RTOS; under ESP-IDF, identify
a live source at runtime rather than from `soc/interrupts.h`.

## Failure signatures `(partly measured)`

Config for the `test/` build: `CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT=y` with
`CONFIG_ESP_SYSTEM_PANIC_REBOOT_DELAY_SECONDS=0`, plus
`CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH` and `CONFIG_ESP_COREDUMP_CHECK_BOOT`.
So a panic **prints and reboots immediately** - it does not hang, and it does
not sit waiting for GDB. `(measured, from sdkconfig)`

The panic text itself has **not** been observed on this board; no fault was
provoked in either session. `(assumed)`

### The core dump partition is a reliable after-the-fact fault detector

Because `CHECK_BOOT` is set, every boot prints one of:

```
I (663) esp_core_dump_flash: Found core dump 25476 bytes in flash @ 0x3b0000
```

or, when the partition is empty, nothing after the `Found partition 'coredump'`
line. `(measured - present at session start, absent after an erase, absent
again after 12 provoking runs)`

This makes "did the board actually fault?" answerable **without watching the
console live**: erase the dump, run the thing under test, reboot, look for the
line. Absence of a core dump after a run is positive evidence that no panic
occurred - unlike console silence, which proves nothing.

Useful symbols for trapping a fault before it reboots (addresses change every
build - re-derive with `xtensa-esp32-elf-nm <elf> | grep -iE 'panic_handler|abort'`):

```
401bc9bc T esp_panic_handler      # panic.c:295
4009281c T abort
400d835c t panic_handler
400e37d0 T esp_core_dump_write
```

Two `hbreak`s on `esp_panic_handler` and `abort` are enough to catch a fault
with a live backtrace instead of a printed one.

## Wrong turns

1. **The first DROM/flash read after a breakpoint stop returns garbage, and
   the garbage is the address itself.** `(measured, reproduced twice)`

   Immediately after `continue` hit `hbreak app_main`:

   ```
   x/1xw 0x3F400000   ->  0x3f400000     # value == address
   x/1xw 0x3F400020   ->  0x3f400020     # value == address
   ```

   after `maintenance flush dcache`, and on every read thereafter:

   ```
   x/1xw 0x3F400020   ->  0xabcd5432     # correct (app_desc magic)
   ```

   Anything that forces a full target-state refresh clears it too - an
   `info threads` was enough. `maintenance flush register-cache` alone is
   **not**.

   This is the nastiest trap on this target because the bad value is plausible:
   a pointer-shaped word at a pointer-shaped address reads as real data, not as
   an error. Contrast the honest failure mode when DROM genuinely is not mapped
   (halted in ROM straight after `monitor reset halt`), which returns
   `0xbad00bad` and is unmistakable.

   **Rule: put `maintenance flush dcache` after every stop, before reading any
   flash or DROM address.**

2. **Reading DROM straight after `monitor reset halt` killed the GDB session.**
   A `printf "%s", ((char*)0x3F400030)` while halted in ROM (flash cache
   unmapped) dropped the connection - OpenOCD logged
   `Error on socket 'GDB': errno==54 ... dropped 'gdb' connection` - and every
   remaining command in the batch file was silently skipped. Read mapped
   addresses only after the target is past the bootloader.

3. **One bad `monitor` command aborts the rest of the batch script.**
   `monitor esp32.cpu0 riscv info` (wrong architecture - this is Xtensa) failed
   with `Protocol error with Rcmd: 01`, which stopped the script before its
   `monitor resume`, leaving the board halted for the next session. Put cleanup
   (`delete`, `monitor reset run`) where a mid-script abort cannot skip it, or
   verify `monitor` subcommands exist first.

4. **Loading the 15 MB ELF is slow.** A foreground GDB run exceeded a 120 s
   tool timeout on the first attempt. Run these in the background from the
   start.

5. **A breakpoint on an assert line stops *at the branch*, after the operand
   has been loaded.** `(measured)` Breaking on
   `TEST_ASSERT_NOT_EQUAL(0, information->IsValid)` put `$pc` on
   `bnez.n a8, ...` - the load `l32i` had already executed. Patching
   `information->IsValid` in memory at that stop therefore changed nothing and
   the assertion still failed, which reads exactly like "the write did not
   land".

   `x/8xb` before and after proved the write *had* landed. The fix is to patch
   the register the branch actually tests (`set $a8 = 1`), or to break a line
   earlier. **Always `x/12i $pc` before concluding a memory patch was ignored.**

   Xtensa windowed registers behaved normally here: `$a8` read and wrote
   correctly at frame 0, no frame-relative surprise.

## Driving the Unity interactive test menu `(measured)`

The `test/` build boots to a numbered menu on the console. It can be driven
entirely non-interactively: hold `/dev/cu.usbserial-101` open, reset through
OpenOCD's telnet 4444, then write `<n>\r\n`. Output comes straight back, and a
Unity `TEST_ASSERT` failure prints `file:line:name:FAIL:...`, which is usually
enough to locate a failure without attaching GDB at all.

Note the reported "Test ran in N ms" stays small even across a JTAG halt -
OpenOCD freezes the timers while halted, so elapsed time is **not** evidence
about whether the debugger stopped the target.
