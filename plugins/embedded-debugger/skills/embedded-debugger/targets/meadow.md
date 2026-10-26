# Target profile: Wilderness Labs Meadow F7 (two MCUs on one board)

A Meadow board is **two processors that are debugged separately**:

| | Part | Runs | Probe | Repository |
|---|---|---|---|---|
| Application | **STM32F777ZIT6** (Cortex-M7 r1p0) | Meadow.OS on NuttX | ST-Link/V3M, SWD | `OS/Rebase` or `OS/Meadow.OS` (Legacy: `OS/Legacy` or `OS/Meadow`) |
| Coprocessor | **ESP32-PICO-D4** (Xtensa LX6, dual core) | MeadowComms | ESP-Prog, JTAG | `OS/Meadow-ESP32` |

The STM32 checkouts go by more than one directory name: Rebase may be
`OS/Rebase` or `OS/Meadow.OS`, and Legacy may be `OS/Legacy` or `OS/Meadow`.
Tell them apart by what is in the tree, not the name -- Legacy carries the
`openocd` submodule and enables semihosting in its OpenOCD config (below).

Read the per-chip files first -- this one records **only what comes from the two
being on one board**, which is where the time goes:

- STM32 side: `stm32-family.md`
- ESP32 side: `esp32-family.md`, then `esp32-pico-d4-espprog.md`
- **Reading the ESP32 console** (`+++` control markers, message dumps, core dump
  traffic): `meadow-esp32-trace.md`. Summarise a capture with
  `../tools/meadow_esp32_log.py` rather than reading it by eye.

> **Provenance.** `(measured)` = observed on this bench on 2026-09-20, board
> attached with both probes. `(repo)` = read out of the project's own configs
> or source, which is stronger than recall but is not the same as watching it
> happen. `(assumed)` = neither -- check it the first time it matters.
>
> `(repo)` is an addition to the template's two markers. It earns its place
> here because much of what this board does is pinned down by checked-in
> OpenOCD and GDB configuration, and collapsing that into `(assumed)` would
> throw away the distinction that matters.

---

## The first trap: there are three OpenOCD builds `(measured)`

They are not interchangeable, and the one on `PATH` is the wrong one for both
STM32 repositories.

| Side | Build | Selected by |
|---|---|---|
| STM32, Rebase | `~/GitHub/ThirdParty/openocd` | `MEADOW_OPENOCD_DIR` |
| STM32, Legacy | `openocd/` in the Legacy checkout (`OS/Legacy` or `OS/Meadow`) -- a git submodule, built in the repository | repo-local path |
| ESP32 | Espressif's `openocd-esp32` fork, from the ESP-IDF tool install | first on `PATH` |

Version in use for the STM32 `(measured)`: `0.12.0+dev-02429-ge4c49d860
(2026-04-03)`.

**Legacy and Rebase need different builds because the NuttX decoding differs in
each tool.** The two repositories carry different NuttX versions, and an
OpenOCD build decodes task state against the layout it was built for. This is
the expensive kind of mismatch: a build pointed at the wrong NuttX does not
refuse the connection, it returns a **thread list that looks plausible and is
wrong** -- task names from the wrong offsets, states that never change,
stacks attributed to the wrong task. The same class of failure as loading
symbols that do not match the firmware (below), reached by a different route,
and worth suspecting the moment a task list stops making sense.

So: one OpenOCD per repository, and neither is the one on `PATH`.
`scripts/debug_methods.sh` refuses to run when `MEADOW_OPENOCD_DIR` is unset,
which is the good case; the bad case is reaching for `openocd` by name and
silently getting the Espressif fork.

## Connection -- STM32 `(repo, confirmed by connecting)`

The supported route is the repository's own scripts, two terminals:

```bash
./debug.sh --server     # OpenOCD with debug.cfg, blocks
./debug.sh              # arm-none-eabi-gdb, loads gdb/.gdbinit, connects
```

What `debug.cfg` sets that differs from a stock STM32 session:

- **`gdb_port 4242`**, not 3333. Connecting to 3333 out of habit reaches
  either nothing or, worse, an ESP32 session left running.
- **`stm32f7x.cpu configure -rtos nuttx`** -- OpenOCD presents NuttX tasks as
  GDB threads. This needs the kernel symbols; without them it degrades noisily.
- **`reset_config none separate`** -- present for ST-Link clones with no RST
  pin. With a genuine V3M it can go; if reset behaves oddly, suspect this line.
- `debug_level 1`, `tcl port 6666`.

**Leaving the board running after a session** `(measured, 2026-10-21)`. A GDB
`detach`, and killing OpenOCD, can both leave the STM32 **halted**: hcom then
stops answering and the CLI reports "Timeout attempting to attach". This
happened twice in one session. Two habits prevent it:

- end every GDB script with `monitor resume` before `detach`;
- after every session, check that the core runs:
  `openocd ... -c init -c "mdw 0xE000EDF0" -c shutdown`. DHCSR bit 17 (S_HALT)
  must be clear: `0x01010001` means running, `0x00030003` means halted.

To recover, use `-c init -c halt -c resume`. A plain `resume` fails with "not
halted" when OpenOCD has not yet seen the halt itself.

**Thread ids are not TCB addresses.** The ids OpenOCD's `-rtos nuttx` shows,
for example `Thread 537215456`, are not `struct tcb_s` addresses, and reading
them as TCBs gives zeros. The reliable route:

- walk `g_pidhash[0 .. g_npidhash)`, where each entry is a `struct tcb_s *`;
- read `task_state` (no SMP: 2 READYTORUN, 3 RUNNING, 5 WAIT_SEM, 6 WAIT_SIG,
  7 WAIT_MQNOTEMPTY), `waitobj`, `sched_priority` and `xcp.regs`;
- the running task is `((struct tcb_s *)g_readytorun.head)`.

A RUNNING state with no saved registers only means that task was on the CPU at
the halt. Sample several times before concluding anything from it.

`bt` on a waiting thread gives `0x00000000` frames here. To see managed stacks
there is no shortcut; bisect with `Console.WriteLine` markers in the app.

Connecting by hand instead (what the measurements below were taken with):

```bash
"$MEADOW_OPENOCD_DIR/src/openocd" -s "$MEADOW_OPENOCD_DIR/tcl" \
  -f interface/stlink.cfg -f target/stm32f7x.cfg \
  -c "reset_config none separate" -c "init; halt"
```

`--stlink` switches `debug.sh` to `st-util` instead, which wants
`stlink/build/Release/src/gdbserver/st-util`; that binary is **not built** on
this bench, so the flag fails rather than falling back.

### Legacy differs from Rebase in one way that changes behaviour `(repo)`

The two `debug.cfg` files are near-identical -- same probe, same target, same
`gdb_port 4242`, same `-rtos nuttx`. The difference is that **Legacy enables
semihosting and Rebase comments it out**:

```tcl
arm semihosting enable          # Legacy: active.  Rebase: commented out
arm semihosting_fileio enable
```

Semihosting halts the core on every call. On Legacy that is the configured
behaviour, so timing measured there is not comparable with timing measured on
Rebase, and firmware that makes semihosting calls will stall on Legacy if no
debugger is attached to service them. See `stm32-family.md` for why this makes
timing-dependent faults disappear.

### What the probe reports `(measured)`

```
Info : STLINK V3J9M3 (API v3) VID:PID 0483:374E
Info : Target voltage: 3.244504
Info : SWD DPIDR 0x5ba02477
Info : [stm32f7x.cpu] Cortex-M7 r1p0 processor detected
Info : [stm32f7x.cpu] target has 8 breakpoints, 4 watchpoints
```

Identification registers, all read halted `(measured)`:

| Register | Value | Means |
|---|---|---|
| `CPUID` `0xE000ED00` | `0x411FC270` | Arm, Cortex-M7, r1p0 |
| `DBGMCU_IDCODE` `0xE0042000` | `0x10016451` | DEV_ID `0x451` = F76x/F77x; REV_ID `0x1001` = rev Z |
| `FP_CTRL` `0xE0002000` | `0x10000080` | 8 breakpoints, FPBv2 |
| `DWT_CTRL` `0xE0001000` | `0x40000000` | 4 watchpoints |
| `AIRCR` `0xE000ED0C` | `0xFA050000` | PRIGROUP 0 -- all 4 bits preemption, no subpriority |

The 8/4 figure matches what `gdb/.gdbinit` declares to the remote, so that
file is telling the truth.

### `VTOR` moves -- read it every time `(measured)`

Two halts on this board, under two images, gave two different answers:

| When | `VTOR` |
|---|---|
| Before NuttX was reflashed | `0x00200000` |
| Running the current NuttX build | `0x08040000` |

**Neither is a property of the board.** An earlier draft of this file recorded
the first as if it were; that was wrong, and this table replaces it.

`0x00200000` is the **ITCM alias** of flash -- the same physical memory as
`0x08000000` via AXIM, `0x07E00000` lower. It is where the part boots, and the
option bytes say so: `OPTCR1 = 0x00400080` -> `BOOT_ADD0 = 0x0080 << 14 =
0x00200000` `(measured)`. So a vector address that looks `0x07E00000` off from
the linker script is the other alias, not a fault.

`0x08040000` is **the NuttX kernel**. An earlier draft guessed "the user image"
from `flash.cfg`'s offsets; that was wrong. The images on disk settle it by
direct comparison `(measured)` -- the first words of `nuttx.bin` are byte-for-byte
the table read from flash at `0x08040000`:

| | first words |
|---|---|
| flash `@ 0x08000000` and `Meadow.OS.bin` offset 0 | `20080000 0801161d 08011509 08011515 ...` |
| flash `@ 0x08040000`, `nuttx.bin` offset 0, **and** `Meadow.OS.bin` offset `0x40000` | `20035620 081661c9 081661d3 081661d3 ...` |

So **`Meadow.OS.bin` is a three-part image**, assembled by `build.sh` at the
end of a build (only for a protected build -- the whole block is skipped when
`CONFIG_BUILD_FLAT=y`):

```
Meadow.OS.Update.bin   1792 KB, zero-filled, then
                         nuttx.bin       (kernel) at offset 0
                         nuttx_user.bin  (user)   from byte 512
                         STM32 CRC written to the last 4 bytes, 0x001BFFFC
Meadow.OS.bin          Meadow.BL.bin  (bootloader) at offset 0
                       Meadow.OS.Update.bin        at offset 0x40000
```

Flashed at `0x08000000`, that gives **bootloader at `0x08000000`** and
**kernel + user at `0x08040000`** -- which is why `VTOR` ends up at
`0x08040000`: the bootloader runs, then hands over to the kernel, which points
`VTOR` at its own table.

The CRC matters when a board will not start. It sits at `0x001BFFFC` inside the
OS image, so **absolute `0x081FFFFC` -- the last word of the 2 MB flash** -- and
is generated by `srec_cat` over the cropped image. A partial or hand-patched
write leaves it stale, and a board that refuses to leave the bootloader with no
console output at all is worth checking against this before anything else.

A fourth artefact, `Meadow.OS.Runtime.bin`, is carved from `nuttx_user.bin` in
the same step: 3072 KB, which is exactly the size of the QSPI `Runtime`
partition it is destined for. It is not part of the internal-flash image.

It also explains why `flash.cfg`'s `nuttx.bin` / `nuttx_user.bin` offsets do not
describe this board: the script writes `Meadow.OS.bin` at `0x08000000` and
**`shutdown`s before reaching them**, so that branch is dead whenever the
combined image is present.

**The two tables differ in kind**, which is what matters when chasing a fault
`(measured)`:

```
@ 0x08000000  (bootloader)    SP 20080000  Reset 0801161d
              NMI 08011509  Hard 08011515  MemMan 0801151d  Bus 08011525  Usage 0801152d
@ 0x08040000  (NuttX kernel)  SP 20035620  Reset 081661c9
              NMI 081661d3  Hard 081661d3  MemMan 081661d3  Bus 081661d3  Usage 081661d3
```

The bootloader has **distinct stubs** per exception. **The NuttX kernel funnels
every exception to one address** -- so on the running system a HardFault and a
MemManage arrive at the same code, and the vector alone will not tell you which
fired. Read `CFSR`/`HFSR` for that.

Which of the two is live tells you where a fault happened: a fault taken with
`VTOR = 0x08000000` is in the bootloader, before the kernel ever ran.

**The kernel ELF is `nuttx/nuttx.elf`** `(measured)`. The build target is
`nuttx.elf` (`make -C nuttx MEADOW_OS=1 nuttx.elf`), and nothing produces a file
called `nuttx/nuttx` -- so a search for that name concludes "the kernel was never
built" when it was. `gdb/.gdbinit` has this right and loads `../nuttx/nuttx.elf`;
`Documentation/Debugging.md` has it wrong in two places, both its Prerequisites
and its `load-nuttx-symbols` listing.

Do not reach for `nuttx_user.elf` for a kernel address: its `.text` runs
`0x08040238`-`~0x08105840`, `addr2line` returns `?? ??:0` for anything in the
kernel, and its entry `0x0808b2d1` is `hcom_main` in
`apps/system/hcom/hcom_startup_manager.c:126` -- the user side, not what `VTOR`
names.

With the kernel ELF, the vector table decodes `(measured)`:

| Address | Symbol |
|---|---|
| reset vector | `start`, `arch/arm/src/armv7-m/arm_vectors.c:65` |
| every fault vector | **`exception_common`**, `arch/arm/src/armv7-m/arm_exception.S:126` |

So the "all faults go to one address" seen above is not a crude shared stub, it
is **NuttX's standard ARMv7-M dispatcher**: `exception_common` saves context and
dispatches on the exception number. Expect it on any NuttX Cortex-M target, and
read `CFSR`/`HFSR` rather than the vector to learn which fault fired.

## Console and ports `(measured)`

**Find ports by USB identity, not by name.** macOS builds the node name from
the USB location (and, for some devices, the serial number), so names change
when a hub is added, removed or re-enumerates. This happened mid-session on
2026-10-14 with nothing replugged on purpose. The vendor and product IDs do not
change:

```
python3 ~/.claude/skills/embedded-debugger/tools/meadow_ports.py        # list roles
python3 ~/.claude/skills/embedded-debugger/tools/meadow_ports.py esp_console
```

`python3 -m serial.tools.list_ports -v` shows the same identities by hand
(`hwid: USB VID:PID=... SER=... LOCATION=...`).

| Role | USB identity (stable) | Names seen (location-derived; will change) | What |
|---|---|---|---|
| `meadow_cdc` | `2E6A:0001` "Meadow F7 Micro", serial number = board ID | `cu.usbmodemMeadowApp1`; `cu.usbmodem<serial>1` (2026-10-14) | Meadow USB CDC: HCOM, Meadow CLI, `meadow listen`. **Absent** until the OS runs hcom, and during a reset loop |
| `meadow_cdc2` | `2E6A:0001`, second node of the same board | `cu.usbmodem<serial>3` (`cu.usbmodem337B317434383`, 2026-10-26) | **Only in a `build.sh --dualcdc` build.** Without `--nsh` it carries the **syslog**; with `--nsh` it is the NSH console. See "The second USB CDC port: syslog or NSH" below |
| `meadow_dfu` | `0483:DF11` "STM32  BOOTLOADER" (not a serial port; found in `ioreg -p IOUSB`) | n/a | The STM32 ROM bootloader: the board is in DFU mode and Meadow.OS is not running (SWD shows PC in `0x1ff0xxxx`). `flash.sh` over SWD still works |
| `stlink_vcp` | `0483:374E` "STLINK-V3" | `cu.usbmodem21112203`; `cu.usbmodem21203` (2026-10-14); `cu.usbmodem1203` (2026-10-15) | ST-Link virtual COM port. Was the NuttX syslog on 2026-09; see the caveat below |
| `espprog_jtag` | `0403:6010` "Dual RS232-HS" (FTDI FT2232H), interface A | `cu.usbserial-100`; `cu.usbserial-21112100`; `cu.usbserial-21100`; `cu.usbserial-1100` | ESP-Prog JTAG; claimed by OpenOCD over libusb |
| `esp_console` | `0403:6010`, interface B (the second node of the pair) | `cu.usbserial-101`; `cu.usbserial-21112101`; `cu.usbserial-21101`; `cu.usbserial-1101` | ESP32 console direct, 115200 8N1, and the `+++` control markers |
| `uart_sniffer1`, `uart_sniffer2` | `1A86:55D4` "USB Single Serial" (WCH CH343); two on 2026-10-26, serials `559C000220` and `559B012424` | `cu.usbserial-559C0002201`, `cu.usbserial-559B0124241` (2026-10-26) | **Optional, wired by the user on request.** RX tapped onto one line of a board UART. On 2026-10-26: `...559C...` on the board TX into the cell modem (mikroBUS 1) and `...559B...` on the modem TX. Passive; see "Cell modem" below |

**SWD "Examination failed" recovery** `(confirmed by the user, 2026-10-16)`:
OpenOCD sometimes reports `Error: [stm32f7x.cpu] Examination failed` and keeps
doing so at lower adapter speeds and after a CLI reset. Do not keep retrying:
ask the user to put the board in DFU mode. SWD then connects, and `flash.sh`
plus `verify_image` work; follow with `reset run` (or a CLI reset) to boot the
OS.

The failure is intermittent `(measured, 2026-10-18)`. That day `flash.sh`
reached a running OS over SWD three times in a row, and failed once, with the
board running an older build. Try once; if "Examination failed" appears, ask for
DFU mode rather than retrying.


How to read the names: `usbserial-<location><interface>` for the FTDI pair (the
last digit is the interface, 0 = A, 1 = B); `usbmodem<location><n>` for the
ST-Link; for the Meadow CDC either a fixed name or `usbmodem<serial><n>`. The
location part grows by two digits for each hub level (`21112...` behind one more
hub than `21...`). Both `tty.*` and `cu.*` nodes exist for each device.

After the names changed, `meadow port list` reported **"No attached devices
found"** although the Meadow port worked. `meadow config route <cu.* node>`
pointed the CLI at it. If the CLI times out unexpectedly, re-run
`meadow_ports.py` before suspecting the board.

**Caveat** `(measured, 2026-10-14)`: in the new layout `stlink_vcp` gave 0 bytes
during a reset loop that was logging into the RAM buffer, so it was not
confirmed to carry the syslog there. Whether the VCP carries USART1 depends on
how the ST-Link is wired to the board. The boot banner's `to:` field (for
example `to:none (ramlog)` or `to:Host+UART (ramlog)`) says where the syslog is
being sent.

**When no port shows the syslog, read the RAM log over SWD** (see "The syslog
is a RAM buffer" below); it needs neither USB nor a UART.

Use the `cu.*` node, not `tty.*` `(measured)`. Both exist for the ST-Link VCP;
`cu.*` is the callout device and opens without waiting on carrier detect.
115200 8N1 works, and a plain `serial.Serial(port, 115200)` reads it.

### One port can give you both processors -- when the relay is on `(measured)`

The NuttX syslog **can relay the ESP32's console into its own output**, tagged
`ESP Log:`. **This is not always enabled.** It was on for the capture below and
is off on other boards and configurations, so confirm it is present before
relying on it -- and do not read its absence as a dead ESP32.

```
[    0.769000] [10] Meadow 1.99.0.19595 rebooted, H/W:F7CoreComputeV2, Mono:Enabled, Trace level:0x7f, to:Host+UART (ramlog)
[    0.774000] [11] ESP Log: ets Jun  8 2016 00:22:57
[    0.779000] [11] ESP Log: rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
[    1.305000] [11] ESP Log: I (518) app_init: Project name:     MeadowComms
```

When it is on it is the most useful instrument on the board: one port, opened
once, gives a **time-ordered interleaving of both processors against one
clock** -- exactly what the SPI link's failures need, and what two separately
captured logs cannot give you, since nothing ties their timestamps together.

It is a relay through the STM32, not a tap on the wire, which has two
consequences. Its absence means the relay is off or the STM32 is not running
it, **not** that the ESP32 is silent. And with the STM32 halted the relay stops
with it, so ESP32 output during an STM32 breakpoint does not appear here.

Use the ESP-Prog console (`usbserial-...101`) when the relay is off, when the
ESP32 must be watched with the STM32 halted, or when the relay itself is under
suspicion. That path is direct from the ESP32 and does not depend on the STM32
at all.

**That console is also the ESP32 -> STM32 control link** `(repo)`. The `+++RST`,
`+++RDY`, `+++SIR`, `+++MW` and `+++BSY` markers in it are `printf`s the STM32
parses, not log noise. Decoding them, and the rest of the ESP32's output, is
covered in `meadow-esp32-trace.md`.

Line format: `[ seconds.micros ] [ task ] message`. The bracketed number is the
NuttX task, so `[5]`, `[10]` and `[11]` above are three different tasks.

### The second USB CDC port: syslog or NSH `(per the maintainer; measured 2026-10-18 and 2026-10-26)`

`build.sh --dualcdc` gives the board a **second USB CDC port** (`meadow_cdc2`,
`cu.usbmodem<serial>3`, for example `cu.usbmodem337B317434383`) beside the HCOM/CLI port
(`<serial>1`). The NuttX config has `CONFIG_USBDEV_COMPOSITE=y` and `CONFIG_CDCACM_COMPOSITE=y`.
What the second port carries depends on `--nsh`:

| Build | Second CDC port carries |
|---|---|
| `--dualcdc --nsh` | The NSH console (`ps`, procfs); see "Thread names, `ps`..." below |
| `--dualcdc` without `--nsh` | **The NuttX syslog.** This is the way to get the syslog when COM1 (USART1) is needed for something else, such as the cell modem on mikroBUS 1 |

When changing these options, do a full build (`build.sh --clean --force --dualcdc`), as the maintainer instructed; a plain incremental build was not tried.

**Turning the syslog output on.** Use `meadow trace level 2`, or put this in
`meadow.config.yaml`:

```yaml
InternalDebug:
    TraceLevel: 2
```

**Leave `Uart1Use` out of the file**, or the output goes to COM1 instead. On 2026-10-26 I also
kept the word out of comments, to be safe. The boot banner still says `to:none (ramlog)` while
the port is carrying the syslog.

**Capturing across a reset** `(measured, 2026-10-26)`:
- The node disappears during a reset, so opening it before the reset fails. Start the reset
  (SWD `reset run`), poll for the node, open it as soon as it exists, and keep reading.
- The port reappeared about 0.3 s after the reset. The read still began at the boot's
  `0.067 s` lines (`meadow_upd_initialize`), because the syslog is held in the RAM log.
- 115200 in pyserial is fine; the baud rate is ignored on a CDC port.

```python
ser = None
subprocess.run([...openocd... "init; reset run; sleep 500; shutdown"])
while time.time() - t0 < seconds:
    if ser is None:
        if os.path.exists(cdc2):
            try: ser = serial.Serial(cdc2, 115200, timeout=0)
            except Exception: pass
        time.sleep(0.01); continue
    log += ser.read(4096)
```

**`meadow runtime enable` did not survive a following `meadow file write`, twice**
`(measured, 2026-10-26)`. Both times enable reported ENABLED, a config file was written
afterwards, and the next boot logged `Mono:Disabled` and `Mono is disabled`. Running enable
again, then a reset, worked. The cause is not confirmed. **Write files first and enable the
runtime last**, and read the banner's `Mono:` field from this port before debugging anything
that needs Mono.

### Which UART, and the path that survives a fault `(measured)`

**USART1 is the console.** `CONFIG_USART1_SERIAL_CONSOLE=y` in `nuttx/.config`
and in every board defconfig, and the peripheral confirms it: `CR1 = 0x0000002D`
= `UE|RE|TE|RXNEIE`, `BRR = 0x3AA` (938) -> 115200 on a 108 MHz APB2.

**UART5 is also live**, identically configured -- `CR1 = 0x0000002D`,
`BRR = 0x1D5` (469) -> 115200 on a 54 MHz APB1. It is the other end of the
control-and-state link to the ESP32. Every other USART/UART reads `CR1 = 0`, so
exactly two are running:

| | CR1 | BRR | Baud | Role |
|---|---|---|---|---|
| USART1 `0x40011000` | `0x2D` | 938 | 115200 | NuttX console / syslog -> ST-Link VCP |
| UART5 `0x40005000` | `0x2D` | 469 | 115200 | link to the ESP32 |
| USART2/3/6, UART4/7/8 | `0x00` | - | - | off |

With a cell modem on mikroBUS 1, USART1 (COM1) carries the modem instead. In
that case `InternalDebug` must be removed from `meadow.config.yaml`, and the
console termios must not be left on the modem tty; see "Cell modem" below.

The **polled** write path is `arm_lowputc()` -- see `stm32-family.md` for the
busy-wait loop. It is the one to report through from a fault handler, because
it does not depend on the interrupt that a fault has just stopped.

**Faults print a stack dump**: `CONFIG_ARCH_STACKDUMP=y`. **Divide-by-zero and
unaligned access do not fault at all** until the `enable_hardfault` macro in
`gdb/.gdbinit` sets the trap bits -- measured `CCR = 0x00070200`, both traps
clear. And BusFault and UsageFault are not separately enabled
(`SHCSR = 0x00010000`, MemManage only), so both escalate to HardFault.

### What you can see is configured in `meadow.config.yaml` `(measured)`

The board reads `meadow.config.yaml` from its own file system at startup, and
**that file decides what reaches which port**. Everything observed in this
profile follows from it. Read the live values without touching the board:

```bash
meadow device config show                                    # parsed values
meadow file read meadow.config.yaml /absolute/path/out.yaml  # the raw file
```

The five settings that govern visibility:

| Setting | Effect |
|---|---|
| `CoProcessor.LogDestination` | `UART` sends the ESP32's log messages to the NuttX UART -- **this is what produces the `ESP Log:` lines** |
| `CoProcessor.LogComponents` | which ESP components may send to the STM32 at all; filtered at source, so anything not listed is not merely hidden, it is never sent |
| `InternalDebug.Uart1Use` | `trace` routes syslog to the STM32 UART that reaches the debug probe |
| `InternalDebug.TraceLevel` | how much detail. **Valid values 0-3; 2 is usually enough** |
| `InternalDebug.CopyApplicationOutputToUart` | copies managed application output to the same UART. No managed code is present yet, so it has no visible effect today |

**The file's own comment for `TraceLevel` says `Valid values: 1-4` and is
wrong** `(measured)` -- the range is 0-3. Do not set 4.

**Applying a change** `(per the maintainer, 2026-10-22)`. Edit the app
project's `meadow.config.yaml`, then either:

- write just that file to the board and restart the app; or
- redeploy the app with `meadow app run`, which writes the project's copy.

**The file belongs to whichever app was deployed last.** A deploy replaces it,
and removes files that are not in the new deployment. If you deploy a probe app
with a minimal `meadow.config.yaml` in between, the board loses the app's
`CoProcessor` logging settings and `Network: CountryCode`.

`Network: CountryCode: GB` is needed on this bench: without it the hidden
channel-13 network goes Connecting -> Disconnected.

For ESP32 logging settings (`LogComponents`), see `meadow-esp32-trace.md`.

Measured on this board, which is exactly why the earlier captures look as they
do -- `LogDestination: UART` gave the `ESP Log:` relay, `LogComponents:
WiFi;System` is why only those components appeared, and `Uart1Use: trace` put
syslog on the ST-Link VCP:

```
CoProcessor    AutomaticallyStartNetwork=false  LogDestination=UART
               LogComponents=WiFi;System        MaximumRetryCount=7
InternalDebug  Uart1Use=trace  TraceLevel=2  CopyApplicationOutputToUart=true
Device         Name=WiFiBasics  SdCardPresent=false  ReservedPins=C6;C7
Network        DefaultInterface=WiFi  CountryCode=GB  NtpRefreshPeriodSeconds=3600
```

`Network.CountryCode` is worth noting in passing: `GB` sees hidden access
points where `01` does not, so it changes what the WiFi tests can find.

### This all depends on the board booting far enough `(per the maintainer)`

`meadow.config.yaml` is read **after kernel startup**. A board that dies before
that point never applies any of it.

So **silence is not evidence that a setting is wrong**. It is equally
consistent with the board never having reached the code that reads the file,
which is the more serious fault and the one to rule out first. Establish that
the board boots past kernel startup -- the `Meadow.OS initialization began at`
banner is the marker -- before changing any of these settings to chase missing
output. Changing configuration to fix an early-boot hang produces a differently
configured board that still hangs.

The host CLI offers a runtime equivalent when the file is not the thing to
change:

```bash
meadow trace level 2 && meadow uart trace enable
```

Either way, **check the banner first**: the reboot line states the live
setting, and `Trace level:0x7f, to:Host+UART (ramlog)` means output is already
on and neither route is needed. On this bench it was already enabled and a
reset alone produced full output.

### What a healthy reset looks like `(measured)`

```
Meadow.OS initialization began at 2026-09-20 16:22:47 Meadow UTC time.
[    0.067000] [ 5] meadow_upd_initialize
...
[    0.552000] [ 5] Meadow reset code: 0x14, reset count: 90, power cycle count 88
[    0.766000] [ 5] Mono runtime was not found flashed in external flash.
[    0.769000] [10] Meadow 1.99.0.19595 rebooted, H/W:F7CoreComputeV2, ...
```

`Meadow.OS initialization began at` is the first line; anything before it is
left over from the previous run. The banner names the hardware variant --
**`F7CoreComputeV2`** on this board, which is not the only Meadow there is, so
do not generalise pin-level facts from it. It also states the live trace
setting -- `Trace level:0x7f, to:Host+UART (ramlog)` here -- which is where to
look before concluding anything from silence, including about the `ESP Log:`
relay above. `Meadow reset code`, reset count and power cycle count persist
across resets and are the quickest check that a board is rebooting when it
should not be.

### Which clock a Meadow timestamp is in `(per the maintainer, 2026-10-18)`

The board has three different time sources, so a time it reports may not be the
time you think:

- **Syslog `[    0.552000]` prefixes:** always seconds since the last reboot,
  never wall-clock time. Line up two captures from these counters or from host
  time; never from each other's dates.
- **The wall clock after a fresh power-up:** set from the date and time **the OS
  was built**, not the real time. The `Meadow.OS initialization began at ...
  Meadow UTC time` banner, file timestamps and app `DateTime.Now` all start
  there. It is the build machine's local time (confirmed by the maintainer), so
the banner's "UTC" is wrong until NTP corrects the clock.
- **The wall clock after NTP:** when `meadow.config.yaml` asks for network time
  at startup (`Network: GetNetworkTimeAtStartup`, the `NtpServers` list,
  `NtpRefreshPeriodSeconds`), the OS fetches UTC from the time servers once WiFi
  has connected to an access point. If that succeeds, the clock jumps to real
  UTC.

So in one run, app times can sit at build time first and jump forward once the
network is up. For elapsed times inside an app, use `Stopwatch` or
`Environment.TickCount64`, not differences of `DateTime`. To compare with host,
GitHub or CI times, convert everything to UTC first: the host Mac runs in
BST/GMT, and GitHub reports UTC (`...Z`).

## The rule that makes this board different

**Halting one MCU does not halt the other, and neither notices.**

The STM32 is the SPI **master** and initiates everything; the ESP32 is a slave
(`spi_slave_initialize`, `VSPI_HOST`) and only ever responds `(repo)`. So:

- Break in the ESP32's SPI path and the STM32 does not stop -- it keeps issuing
  requests, times them out, and may decide the coprocessor is dead and reset
  it out from under the debugger.
- Break on the STM32 and the ESP32 sits in its slave queue waiting for a
  transaction that never comes, which is indistinguishable from idle.

**Resetting the STM32 restarts the ESP32 too** `(measured)`. A bare
`init; reset run` over SWD -- with `srst` not even connected -- produced
`rst:0x1 (POWERON_RESET)` on the ESP32 about 0.77 s into the STM32's boot. The
STM32 drives the coprocessor's reset during its own initialisation, so there is
no such thing as restarting only the application processor. Any ESP32 state
being accumulated is lost when the STM32 is reset, and an ESP32 GDB session
will lose its target.

A breakpoint anywhere near the link therefore changes the thing being measured.
Prefer, in order: log rather than halt; halt the **master** first so the slave
goes quiet cleanly; or use the after-the-fact route in
`esp32-pico-d4-espprog.md`, where the ESP32 core dump partition records a fault
that has already happened without anything having to be halted while it does.

### Link facts worth having to hand

| | |
|---|---|
| Control and state | UART, STM32 -> ESP32 `(repo)` |
| Requests and responses | SPI, STM32 master `(repo)` |
| ESP32 SPI pins | MISO `GPIO19`, MOSI `GPIO23`, CLK `GPIO18`, CS `GPIO5` `(repo)` |
| SPI transaction limit | **4092 bytes**, a hardware constraint `(repo)` |
| Message payload maximum | 8192 bytes, a protocol invariant not expressed in the code |
| ESP32 console TX | `GPIO1`, shared with UART0 TX `(repo)` |
| `GPIO3` | nominally UART0 RX, repurposed for signalling `(repo)` |

**A poll request that never returns is not a hang.** Poll requests are given an
infinite timeout deliberately; the STM32 owns their setup and teardown. Chasing
one as a stall is wasted effort -- establish first whether the request is a poll.

**A request buffer may be reused as its own response.** Handlers recycle the
request message into the response, so a buffer inspected after dispatch is not
necessarily the request that was sent.

## NuttX specifics `(repo)`

Kernel and user are separate binaries with separate symbols:

| Image | Flashed at | Symbols |
|---|---|---|
| `nuttx/nuttx.bin` (kernel) | `0x08000000` | `nuttx/nuttx` |
| `nuttx/nuttx_user.bin` (user) | `0x08040000` | `nuttx/nuttx_user.elf` |
| `nuttx/Meadow.OS.bin` | `0x08000000` | - |

Each repository has its own **`flash.sh`** that writes the OS to the board,
driving `flash.cfg` over the same ST-Link. Use the one belonging to the
repository being worked in, for the same reason the OpenOCD builds do not
interchange. The Rebase documentation also describes a `--dfu` route via
`dfu-util` for when the board is in DFU mode rather than reachable over SWD.

**Check both symbol files exist before starting.** On this bench
`nuttx/nuttx_user.elf` is present and **`nuttx/nuttx` is not** `(measured)`, so
a session started now would come up with user symbols only and kernel frames as
bare addresses -- which reads exactly like a corrupt stack.

### The external QSPI flash is a separate world `(measured)`

The internal flash at `0x08000000` holds the OS. Everything else lives in a
**64 MB external QSPI part** with its own MTD partition table, printed at every
boot:

| # | Partition | Size | Byte offset |
|---|---|---|---|
| 0 | Runtime | 3072 KB | `0x00000000` |
| 1 | Meadow update | 1792 KB | `0x00300000` |
| 2 | OTA State | 4 KB | `0x004c0000` |
| 3 | OS Persisted | 4 KB | `0x004c1000` |
| 4 | **Assert Data** | 32 KB | `0x004c2000` |
| 5 | Unused | 216 KB | `0x004ca000` |
| 6 | File System | 60416 KB | `0x00500000` |

Geometry: 16384 erase sectors, 4096-byte erase, 256-byte page.

Offsets in this table are **QSPI-relative and start at zero** -- they are not
CPU addresses and have nothing to do with `0x08000000`. Reading one as the
other is an easy and confusing mistake.

### `Assert Data`: nothing writes it `(measured)`

It looks like the STM32's answer to the ESP32 core dump partition -- 32 KB that
would survive the reset which produced it. **It is not one. In this build
nothing writes it at all**, and a crash leaves it untouched.

The mechanism exists and is correctly aimed, which is what makes the gap easy
to miss. All of it lives in `F7MeadowOS/`, outside the `nuttx/` tree:

| Piece | Where | State |
|---|---|---|
| Writer | `hcom_nx_exec_ex_flash_write_assertion_data()`, `hcom_nx/create_fs/hcom_nx_fs_flash.c:1187` | implemented |
| Location | `hcom_nx_exec_ex_flash_assert_data_location()`, same file | implemented, and correct |
| Reader | `hcom_nx_exec_ex_flash_read_assertion_data()` | implemented, **no callers** -- the source says so in a comment |
| Caller of the writer | `hcom_nx/create_fs/hcom_nx_fs_flash_tests.c:516` | **a flash test, and the only one repo-wide** |
| NuttX crashdump hook | - | `CONFIG_BOARD_CRASHDUMP_NONE=y`, and no `board_crashdump` is defined in `F7MeadowOS/` or the board directory |

The location function computes `MONO_RUNTIME (0x300000) + OS_UPDATE (0x1C0000)
+ 2 x erase block (0x2000)` = **`0x4C2000`**, exactly the offset the boot log
prints. So the partition is real, reserved and correctly targeted -- and the
only thing that has ever written to it is a test.

Its documentation says "the assertion data from `up_assert`", and that is the
intent, not the behaviour: `up_assert` has no path to the writer, because the
hook that would provide one is compiled out and unimplemented.

**Consequences for a session:**

- Do not go looking there after a crash. Nothing put anything there.
- `CONFIG_ARCH_STACKDUMP=y` means a fault prints a stack dump to the console,
  and on this board **that console output is the whole of the record** -- if it
  was not captured live, it is gone. Hold the port open across the fault.
- The ESP32 core dump partition remains the only working after-the-fact fault
  record on the board. That asymmetry is worth remembering: the coprocessor
  keeps evidence across a reset and the application processor does not.
- The partition cannot be read over SWD in any case `(measured)`: the external
  QSPI flash is not memory-mapped, and a read of `0x90000000` fails with
  `Failed to read memory`. Reaching it means going through the QSPI driver or
  hcom, not the debugger.

`gdb/.gdbinit` also feeds OpenOCD the NuttX TCB offsets, and it **derives them
from the loaded symbols** rather than hardcoding them:

```gdb
eval "monitor nuttx.pid_offset %d", &((struct tcb_s *)(0))->pid
eval "monitor nuttx.xcpreg_offset %d", &((struct tcb_s *)(0))->xcp.regs
eval "monitor nuttx.state_offset %d", &((struct tcb_s *)(0))->task_state
```

An earlier draft of this file warned that these offsets could go stale when a
NuttX change moved a field. **They cannot** -- literal values are present but
commented out, and the `eval` form recomputes them every session. That is the
correct reading of the file.

The exposure is different and worth knowing: because the offsets come from the
symbols, **loading symbols that do not match the firmware yields a wrong task
list rather than an error**. `monitor nuttx.g_tasklisttable_size 72` is still a
literal, so it can drift. Suspect the symbol files before believing a bizarre
task list.

GDB helpers in `gdb/`: `Nuttx.py` (tasks, stacks), `Nuttx_Tasks.py` (TCB
pretty-printer), `NuttxHeap.py` (heap blocks), `NVIC.py`, `Tracing.py`,
`gdb-dashboard.src`, `ignore_signals.py`, and `STM32F777.svd` for register
decode. They need GDB's embedded Python to match a venv the script builds --
`debug.sh` rebuilds `.venv` when the versions drift, so run GDB through
`debug.sh` rather than directly unless there is a reason not to.

`enable_hardfault` is a `.gdbinit` macro that sets the DEMCR bit trapping
divide-by-zero and unaligned access. Neither faults by default, so a bug of
either kind is silent until this is turned on.

NuttX has the same interrupt-priority ceiling FreeRTOS does, and **no
equivalent of `configASSERT` to catch a violation**. The `freertos` skill's
reasoning about `BASEPRI` and priority bits applies; the automatic detection
does not.

## Boot addresses: what each one selects `(measured)`

`OPTCR1 = 0x00400080` gives `BOOT_ADD0 = 0x00200000` and
`BOOT_ADD1 = 0x00100000`. Both were read back and identified by content:

| | Address | Reads as | Selected when |
|---|---|---|---|
| `BOOT_ADD0` | `0x00200000` | identical to `0x08000000` -- the ITCM alias of application flash | BOOT0 low (normal) |
| `BOOT_ADD1` | `0x00100000` | identical to `0x1FF00000` -- the **ST factory system bootloader** | BOOT0 high |

`0x00100000` and `0x1FF00000` return the same words, and their vectors point
into `0x1FF0xxxx`, so `BOOT_ADD1` is the ITCM alias of system memory rather
than anything of Meadow's.

**So `BOOT_ADD1` is used, and it is the DFU route.** Pulling BOOT0 high boots
the factory bootloader instead of Meadow.OS, which is what makes
`flash.sh --dfu` and `dfu-util` work. It is also the way back from a board
whose flashed image will not start -- and unlike everything in
`meadow.config.yaml`, it does not depend on the application being healthy.

`OPTCR = 0xFFFFAAFD` also carries `RDP = 0xAA`, readout protection **level 0**.
That is why the debugger can read flash at all; a board that suddenly refuses
memory reads has had RDP raised, which is not something a debug session
recovers from without a mass erase.

## Absent by design, not broken `(per the maintainer)`

**Mono and managed code are not in the Rebase firmware yet.** Bringing Mono in
is what the Rebase project is *for*. Until that lands, a cluster of things reads
as failure and is not:

| Symptom | Reality |
|---|---|
| `Mono runtime was not found flashed in external flash` at every boot | Expected. The runtime is a separate artefact, `Meadow.OS.Runtime.bin`, destined for the QSPI `Runtime` partition, and is not flashed |
| `InternalDebug.CopyApplicationOutputToUart: true` produces nothing | There is no managed application output to copy |
| `MonoControl.Options: --debug` in `meadow.config.yaml` has no effect | Nothing consumes it |
| The QSPI `Runtime` partition (3072 KB) is empty | Nothing has written to it |
| The banner says `Mono:Disabled` -- or `Mono:Enabled` | A **kernel build-config flag** (`build.sh -m|--mono`), independent of whether the runtime binary is present. Both states occur on working boards, and neither means the runtime is there |

Do not spend a session on any of these. They are the gap the project exists to
close, not evidence of a fault.

The last row is the one that misleads, because `Mono:Enabled` and
`Mono runtime was not found flashed` are both true at once and look
contradictory. They are answering different questions: one is what the kernel
was built to support, the other is what is actually in external flash.

**`build.sh -m|--mono` cannot be used.** The flag is listed in `--help` and the
script accepts it, but the Mono code is not in the tree, so there is nothing for
it to build. A plain `./build.sh` is the only build available, and the
`Mono:Disabled` image it produces is therefore the correct and expected state of
a board flashed from Rebase today -- not a misconfiguration to be put right.

It follows that **a board reporting `Mono:Enabled` was not flashed from a
current Rebase build.** If that matters for what you are chasing, find out where
its image came from rather than trying to reproduce it here.

## Mono / .NET runtime bring-up on Rebase (NuttX 12) `(measured, 2026-10-14)`

Learned while bringing up the .NET 11 Mono runtime (monovm, JIT and interpreter)
on a CCM (F7CoreComputeV2) under the Rebase OS. Addresses are from one build;
re-derive them with `arm-none-eabi-nm` every time.

### Where things live in a Rebase build

| Region | Contents | Symbols |
|---|---|---|
| `0x0804xxxx`-`0x080fxxxx` | user image (hcom, libc, `meadow_mono_main`) | `nuttx/nuttx_user.elf` |
| `0x0814xxxx`-`0x0819xxxx` | kernel (`arm_hardfault`, syscall stubs) | `nuttx/nuttx.elf` |
| `0xc0000000`... | Mono runtime image in SDRAM (`.mono`, `.mono_data`, `.mono_bss`), copied from QSPI at Mono start | `nuttx/nuttx_user.elf` |

Load both: `arm-none-eabi-gdb nuttx/nuttx.elf` plus `add-symbol-file
nuttx/nuttx_user.elf`. Runtime frames then resolve to runtime source lines.

**The OS image and the runtime image must come from the same build**, not just
the same version string. The SDRAM code calls user-flash functions by address,
so any change to user code moves them. After each OS reflash, write the runtime
too, and delete the staged copy that the write leaves in `/meadow0`. The delete only
keeps the file system clean; it is not needed for the runtime to work:

```bash
meadow firmware write runtime -f nuttx/Meadow.OS.Runtime.bin && meadow file delete Meadow.OS.Runtime.bin
```

(`meadow` here is the 3.0 CLI, `dotnet <meadow-3.0>/Meadow.CLI/Source/Meadow.CLI/bin/Release/meadow.dll`
when it is not on `PATH`.) `./flash.sh --includeruntime` does not do this with the 3.0
CLI: it runs `meadow mono update rt`, which that CLI no longer has, and prints the
help text and `error` `(measured, 2026-10-20)`. Use the command above.

**Reset the board after writing the runtime or the ESP32, before reading
versions** `(measured, 2026-10-15; confirmed by the user)`. The OS reads the
runtime version from the signature block at QSPI offset 0
(`hcom_nx_config_refresh_mono_version`) and the coprocessor version from the
ESP32 at boot, and caches both. After `firmware write runtime`, `device info`
still showed the previous runtime (2.999.50.1 against OS 2.999.50.9) although
the image written was stamped .9; after `meadow device reset` it showed .9.
Mono starts only when all four version fields match
(`hcom_mono_ctrl_do_versions_matched`, build number included), so every OS
rebuild bumps the build number and needs the runtime from the same build. To
check the image itself: `xxd -l 48 nuttx/Meadow.OS.Runtime.bin` shows
`aabbccdd`, then build, revision, minor, major (uint32 LE each) and the branch
name at offset 0x20. Also: a `meadow app run` left attached holds the CDC port,
and later CLI calls fail with "Access to the port ... is denied".

**Stopping `app run` means stopping its `dotnet` child too** `(measured,
2026-10-18)`. Through a wrapper (`m3` -> `exec dotnet meadow.dll`) or `perl -e
'alarm N; exec ...'`, `pkill -f "m3 app run"` matched only the wrapper. The
`dotnet .../meadow.dll app run` process lived on and held the port, and the next
deploy failed with "No device found: Access to the port ... is denied". Find the
holder with `lsof /dev/cu.usbmodem<serial>1`, and stop it with
`pkill -f "meadow.dll app run"`. In a script, start the CLI in its own session
(`subprocess.Popen(..., start_new_session=True)`) and stop it with
`os.killpg()`; `tools/testrunner/testrunner.py` does this.

**Disable the runtime before writing it after an OS reflash** `(measured,
2026-10-15)`. A rebuild keeps the version stamp (the build number is the commit
count), so after flashing a new OS the old runtime still matches it, Mono
starts against the wrong user image, and `firmware write runtime` times out
("Disabling runtime..." then a SerialStream TimeoutException, or "Timeout
attempting to attach"). Reliable sequence: flash and verify the OS, `reset run`,
wait for the CLI, `meadow runtime disable`, then `firmware write runtime`, then
`meadow device reset`.

**Better: disable the runtime BEFORE flashing the OS** `(maintainer advice,
2026-10-25; on this bench the Meadow USB port often did not enumerate after a
flash made with the runtime enabled, and the first runtime write failed with
"Cannot find port")`. When deploying the OS and the runtime together, run
`meadow runtime disable` first, while the old OS is still running, and only
then flash the OS or write the runtime. Full sequence: `meadow runtime disable`,
flash the OS, `reset run` over SWD, check the `/dev/tty.usbmodem<serial>`
port exists, `meadow firmware write runtime`, `meadow file delete
Meadow.OS.Runtime.bin`, `meadow runtime enable`, then `meadow device info` to
confirm the OS and runtime versions match.

**When managed results are numerically wrong, suspect the Thumb-2 JIT encoder**
`(measured, 2026-10-15)`. The runtime's `src/mono/mono/arch/arm/thumb-codegen.h`
is hand-written for the Meadow port; register shifts were wrong (fixed in
runtime 2f38333bddc) and LDR/STR negative offsets before that (522718519a7).
Compare the same app with `--interp` (MonoControl `Options` in
meadow.config.yaml) and isolate the expression with small `NoInlining` methods.

**Prove which engine ran before comparing JIT and interpreter** `(measured,
2026-10-18)`. The mini suite gave 733/2/1 in both modes and took 19 s against
23 s, because each test runs once and JIT compile time dominates, so the timing
told nothing. Time a hot loop in the app instead: 2,000,000 iterations of
`sum += (i ^ (i >> 3)) & 0xff` in a `NoInlining` method took **88 ms under the
JIT and 1861-2514 ms interpreted**. Three more traps:

- **Options are copied with the config.** An app's `meadow.config.yaml` copied
  from an older test app carried `MonoControl: Options: --debug`, so a run
  reported as plain JIT was really `--debug`. Read the `Options` line of every
  config you deploy.
- **The board keeps the last config.** An OS and runtime reflash leaves the app
  files in place, so Mono starts with the last deployed options. NSH `ps` shows
  them as the Mono task's arguments, for example `Mono --interp` or
  `Mono --debug`.
- **An empty `Options:` is YAML null.** For the plain JIT, leave out the
  `MonoControl` section rather than writing an empty value.
- **hcom passes only whitelisted options, and drops the rest silently**
  `(repo + measured, 2026-10-22)`. The whitelist is `_mono_options[]` in
  `apps/system/hcom/mono/hcom_mono_control.c`; the options string is split on
  spaces.
  - Exact matches only: `--interp`, `--aot`, `--jit`, `-v`, `--llvmonly`,
    `--llvmonly-interp`, `--debug`, `--soft-breakpoints`.
  - Prefix matches: `--optimize=`, `--gc-params=`, `--trace=`, `--profile=`.

  Before PR #762, `--interp=jit=<Class>` (Mono's mixed mode) **never reached
  Mono**. A whole morning's "mixed-mode bisection" was really the plain
  interpreter every time. Check the whitelist before trusting any new option.
  The mode probe shows whether `--interp` took effect, but not whether its
  sub-options did.
  - Mono's own `--interp=<opts>` does not override the mode, so #762 also
    appends a plain `--interp`. The boot syslog line `Mono options: ...` shows
    what Mono actually received.
  - Mixed mode also needs runtime fa4051d3836 `(measured, 2026-10-22)`.
    `interp_parse_options` freed the `jit=` class names it had stored, and
    `p (char*)mono_interp_jit_classes->data` read `"\001"`. Read that global
    over SWD to check a sub-option took effect. The match is on the short class
    name. Measured: the named class's loop runs at JIT speed (75-92 ms) and
    another class's at interpreter speed (1.8-1.9 s).
  - `--trace=N:<Namespace>` does pass, and gives method entry and exit without
    changing the generated code.

### Running Mono's mini test suite `(measured, 2026-10-18)`

The runtime's JIT regression tests (`src/mono/mono/mini/*.cs`, 10 suites) run as
a Meadow app, `tests/managed/MiniTests`. `tools/testrunner/testrunner.py`
deploys it once per Mono mode and judges it from `TESTRUN BEGIN/CASE/INFO/END`
lines. Set these in a worktree, where the defaults are one directory off:

```bash
MEADOW_CLI="dotnet <meadow-3.0>/Meadow.CLI/Source/Meadow.CLI/bin/Release/meadow.dll" \
MEADOW_RUNTIME_DIR=<OS>/runtime MEADOW_CORE_DIR=<meadow-3.0>/Meadow.Core \
python3 tools/testrunner/testrunner.py          # results: test-runs/<id>/summary.md
```

The baseline, on hardware, is the same under the JIT and the interpreter:

- **733 ran, 2 skipped, 1 failed.** The failure, `exceptions.test_0_ldflda_null_pointer`, is a
  known failure: Mono throws NullReferenceException for `&ptr->field` through a null
  unmanaged pointer.
- **About 75-115 s per mode**, most of it the deploy.
- **Legacy's 735/732 and 733/2/1 came from the Renode emulator, never from a board.**

A drop from 733, or a crash with no `END` line, is a runtime regression worth
bisecting. The last `Running '<test>' ...` line in the log names the test that
took the runtime down.

### Thread names, `ps` and finding an unnamed thread `(measured, 2026-10-18)`

Build with `./build.sh --nsh --dualcdc`. NSH, with procfs and therefore `ps`,
then runs on the **second** Meadow CDC port: `cu.usbmodem<serial>3` beside the
CLI's `<serial>1`. Without `--nsh`, that port carries the syslog instead; see
"The second USB CDC port: syslog or NSH". Open it with pyserial, send `ps\r` and read for about 4 s.

- **"Resource busy"** right after a board reset was a transient. Retry after a
  few seconds; `lsof` showed no holder. When `lsof` does show a holder, it is
  usually the user's terminal, so ask them to release it.
- **Names.**
  - From runtime 987bd04ff3e (`HAVE_PTHREAD_SETNAME_NP`), Mono's threads are named: `Finalizer`,
    `.NET TP Worker`, `.NET TP Gate`, `.NET Timer`, `.NET Sockets`. Names are cut to 15 characters.
  - The hcom pthreads are named from Meadow.OS #736 on: `EspcpUARTMonitor`, `MonoStdxxx`,
    `HcomRecv`, `Traverse`, `MonoMonitor`.
  - Before that, every Mono thread showed as `Mono` and every hcom pthread as `hcom_main`.
- **Finding an unnamed thread.** For a pthread, `ps` prints the entry address after the name.
  Look it up with the Thumb bit cleared, in the image that is on the board:
  `arm-none-eabi-addr2line -f -e nuttx/nuttx_user.elf $((addr & ~1))`. Every Mono thread shows
  entry `0xc003ef59`, the runtime's thread start in SDRAM, so only its name tells them apart.
- **The crash dump lists every task with its name.** `CONFIG_ARCH_STACKDUMP=y` selects
  `DEBUG_ALERT`, so `dump_tasks()` in `sched/misc/assert.c` prints the list with names and stack
  use. Named threads make that list readable.
- **Priority and stack columns are worth a glance.** Before runtime 82d0a523ea7 (Meadow.OS #698,
  fixed in PR #748) Mono threads ran at 128, above the Mono task (80) and some hcom threads (120),
  and managed `Highest` at 255. Now managed priorities Lowest to Highest are 70, 75, 80, 85, 90,
  with Normal (and the Mono task) at 80. `MonoMonitor` had a 64 KB default stack (#735).
- **Task priorities without NSH, over SWD** `(measured, 2026-10-20)`. Start OpenOCD, then run
  GDB on `nuttx/nuttx.elf` with this script; it reads the kernel's PID table, so it needs no
  `--nsh` build and no second CDC port, and halts the board only for the read:

  ```gdb
  target extended-remote localhost:3333
  monitor halt
  set $i = 0
  while ($i < g_npidhash)
    set $t = (struct tcb_s *) g_pidhash[$i]
    if ($t != 0)
      printf "TASK pid=%d prio=%d name=%s\n", $t->pid, $t->sched_priority, $t->name
    end
    set $i = $i + 1
  end
  monitor resume
  detach
  ```

  Managed `Thread.Name` is the name shown (up to 15 characters), so give a probe app's threads
  distinct short names. `meadow app run <path> -c Release` takes the app path as an argument,
  not `--path`.

### Socket errors as the app sees them `(measured, 2026-10-18)`

- **The wire uses NuttX 7 numbers.** ESP32 errno values and socket levels cross
  the SPI link in NuttX 7 numbering (errno is the same as newlib's), so one
  ESP32 firmware serves both Legacy and Rebase. Rebase converts them in
  `F7MeadowOS/espcp/espcp_mapping.[ch]`. If an error looks wrong in a Rebase
  app, check that mapping before blaming the ESP32.
- **lwIP names some errors differently** from what an application expects.
  These are lwIP's own choices, not mapping bugs; Legacy sees the same:
  - a refused connect gives `ECONNRESET` (`SocketError.ConnectionReset`), not
    `ECONNREFUSED`;
  - an unanswered SYN gives `ECONNABORTED` after about 9 s, not `ETIMEDOUT`;
  - a receive timeout gives `ETIMEDOUT` (110), as expected.
- **`SO_RCVBUF` cannot be read.** `getsockopt(SO_RCVBUF)` fails with
  `ENOPROTOOPT`, because `CONFIG_LWIP_SO_RCVBUF` is off in the ESP32 sdkconfig.
- **Reading `ip=255.255.255.255`** at app start, when WiFi was already
  connected, means the IP was read before it was filled in. nettest, which
  connects itself, reports the right address.

### The ESP32 never joins WiFi: `NO_AP_FOUND` and the country code `(measured, 2026-10-20; cause per the user)`

Symptom: the app starts, Meadow.Core logs `Device is configured to use WiFi` and
`[esp] Current state: Unknown`, and the adapter never connects (`IsConnected`
stays false, no IPv4 address on the wireless interface). There is no "no Default
SSID" warning, so the ESP32 does hold the credentials.

The ESP32 console shows why, when its log is on (`CoProcessor: LogDestination:
UART`, `LogComponents: WiFi;Messages;System` in `meadow.config.yaml`): one
`ConnectToDefaultAccessPoint`, then `WIFI_EVENT_STA_DISCONNECTED` about every
1.4 s, `Reconnecting, retry count 0..6, maximum 7`, then `Not reconnecting`.
Reason code **201 (`NO_AP_FOUND`), RSSI -128** on every attempt (the
`StationDisconnectedEvent: Reason <n> (<name>), RSSI <n>` line is a Meadow-ESP32
trace added 2026-10-20; older firmware prints no reason). `NO_AP_FOUND` is a scan
that saw no matching network, so it is not a wrong password (that would be
`AUTH_FAIL` or a handshake timeout).

**Cause here: the network is hidden and on channel 13, and the ESP32 starts with
country code `01` (world safe mode), which does not scan channel 13.** Set
`Network: CountryCode: GB` in `meadow.config.yaml` and the ESP32 scans channel
13 and joins (measured: `wifi.connected` passed in 3 s, DHCP address assigned). The
ESP32 log shows `SetCountryCode 01` at boot when the key is missing. Check the
country code before suspecting credentials, the router or the firmware.

Also: a deployed app's `meadow.config.yaml` replaces the board's, so a test app
that needs WiFi must ship `Network:` (with `CountryCode`) and `CoProcessor:
AutomaticallyStartNetwork: true`. Without the latter Meadow.Core never starts
the connection.

### The syslog is a RAM buffer: read it over SWD

With `to:none (ramlog)` in the boot banner, the syslog lives in `g_sysbuffer`
(32 KB, kernel `.bss`). It survives everything the CPU survives, so it is the
instrument when USB and UART show nothing: in a reset loop, after a fault, and
when the CLI cannot connect. The layout is `uint32 rl_magic` (`0x12345678`),
`uint32 rl_head` (bytes written), then the circular buffer:

```
openocd ... -c init -c halt -c "dump_image ramlog.bin <g_sysbuffer> 0x8000" -c resume -c shutdown
# decode: head = u32 at offset 4; data = buf[8:8+head] if head <= len else rotate by head % len
```

From inside a GDB stop: `dump binary memory ramlog.bin <addr> <addr+0x8000>`.
Dump it **at** the event (a breakpoint on the reset or fault path), because the
next boot overwrites it.

### Recognising a reset loop

- The ESP-Prog console shows `rst:0x1 (POWERON_RESET)` and `+++RST`/`+++RDY`
  every couple of seconds (2.4 s here). The STM32 resets the ESP32 on each boot,
  so the ESP32 console is the reset counter.
- The Meadow USB CDC never stays enumerated; `meadow` commands time out.
- Two PC samples taken seconds apart land in different stages, for example the
  kernel's `memcmp` under `hcom_nx_exec_ex_flash_copy_blocks_to_memory` (the
  runtime copy into SDRAM), then the bootloader's `VerifyPrimaryImage`.
- `CFSR`/`HFSR` read 0. The reset clears them, so zeros do not mean no fault.
- **A late fault looks like a hang** `(measured, 2026-10-22)`. When Mono faults
  10-15 s into the app, USB stays up long enough each boot that the CLI just
  stops printing. SWD samples then land in normal start-up work, such as JIT
  compiles of `AppContext:TryGetSwitch` again and again. Read the RAM syslog
  banner first: `Meadow reset code: 0x14, reset count: 1430` settled it.
  Reset code `0x14` is software reset (`0x10`) plus the NRST pin bit (`0x04`),
  from `meadow_os.h` (RCC CSR >> 24). A kernel panic after a fault gives it, and
  so does hcom's deliberate reset. To tell them apart, use the breakpoints
  below: `arm_memfault`/`arm_hardfault` versus `meadow_os_reset_board`.

### hcom resets the board whenever Mono exits

`monitor_mono_task` (`apps/system/hcom/mono/hcom_mono_control.c`) waits in
`waitpid()` for the Mono task and then calls the `meadow_os_reset_board` syscall.
A crash, an `abort()` and a clean `return 42` from `Main` all end in a reset, so
**a short test app loops**.

- Catch it at the **user-space proxy** (`meadow_os_reset_board` in
  `nuttx_user.elf`, `proxies/PROXY_meadow_os_reset_board.c`). The backtrace
  then shows `monitor_mono_task`.
- **To break the loop without reflashing**, skip the reset at that breakpoint
  with `set $pc = $lr`. Mono has exited and hcom stays up, so the CLI works
  (disable the runtime, write files).
- Other loop breakers: `meadow runtime disable` (if the CLI gets a window), or
  flash an OS whose version differs from the runtime's (the OS then disables
  Mono itself).
- `meadow file write` disables Mono and reboots before writing (maintainer), so
  run `meadow runtime enable` after every deploy.
- **A CPU fault never reaches that proxy** `(measured, 2026-10-22)`. The kernel
  panics and resets the board directly, so the `meadow_os_reset_board` break
  never fires. What worked instead was stopping Mono from starting:
  `hbreak *<hcom_mono_ctrl_is_mono_enabled>` (a user-flash function; look up
  its address in `nuttx_user.elf`), then `set $r0 = 0` and `set $pc = $lr`, then
  resume. hcom logs "Mono is disabled" for that boot and the CLI can deploy;
  `meadow app run` re-enables the runtime. Two things failed:
  - clearing BK31R bit `0x800` (the runtime-enable flag) from plain OpenOCD
    while the board kept resetting, because `halt` reported "target was in
    unknown state" and the write did not take;
  - `meadow runtime disable` retried in a loop, which never got a window.

### Breakpoints that classify a Mono failure

Hardware breakpoints, all six fit in the FPB together:

| Breakpoint | Means |
|---|---|
| `monoeg_assertion_message` | a runtime `g_assert`; `mono_assertion_message(file, line, condition)` is the caller, so the backtrace names the file, line and condition |
| `abort` | `g_error`/assert path (`monoeg_assert_abort`) |
| `exit` from `nxtask_startup` | `meadow_mono_main` returned normally |
| `arm_hardfault`, `arm_memfault` | a CPU fault (decode below) |
| `meadow_os_reset_board` (user proxy) | the monitor is about to reset |
| `mono_handle_exception` | a managed exception: `r1` is the `MonoObject *`; `p ((MonoObject*)$r1)->vtable->klass->name` and `((MonoException*)$r1)->message` (UTF-16 `chars[]`) |

Mono's own error text goes to stderr, which is lost unless the HCOM redirect is
working, so these breakpoints are often the only way to see the reason.

### Decoding the saved context at `arm_hardfault`

`arm_hardfault(irq, context, arg)`: `r1` is the saved register array. With the
FPU enabled the software part is 28 words (`SW_INT_REGS 12 + SW_FPU_REGS 16`):

| Word | Register |
|---|---|
| 0 | SP at the exception |
| 2-9 | R4-R11 |
| 28-32 | R0-R3, R12 |
| 33 | LR |
| 34 | PC |
| 35 | xPSR |

`printf "%#x %#x\n", ((unsigned*)$r1)[34], ((unsigned*)$r1)[33]`, then
`info symbol` on both and `x/12i` around the PC. Read `CFSR` (`0xE000ED28`) in
the same stop. One measured case: `CFSR = 0x01000000` is UsageFault UNALIGNED,
from an `strd` to a halfword-aligned JIT code address. STRD, LDRD, LDM and STM
always need word alignment, even though plain LDR and STR to the same address
are allowed.

### What is the JIT compiling? `(measured, 2026-10-22)`

Mono's runtime lives in `nuttx_user.elf` with full DWARF (load it with
`add-symbol-file`), so a GDB Python sampler can name the method being compiled.
On each sample, halt, walk `gdb.newest_frame().older()` until
`f.name() == "mini_method_compile"`, then read the method name:
`m = f.read_var("method"); m["klass"]["name"].string() + ":" + m["name"].string()`.
Put the task name (`g_readytorun.head->name`) next to it. Different methods in
every sample, with the same start-up methods coming back
(`AppContext:TryGetSwitch` again and again), mean Mono keeps restarting, not one
stuck compile. A frame chain ending at `0x0` or `??` is JIT code with no unwind
info, which is normal.

### Probing JIT calling conventions from managed code `(measured, 2026-10-22)`

To find a JIT miscompile of arguments, you need neither the debugger nor a
runtime rebuild.
1. Build a `DynamicMethod` that `ldarg`s its parameters and `call`s a
   `[MethodImpl(NoInlining)]` static method that returns what it received. For
   a span, return
   `(nint)Unsafe.AsPointer(ref MemoryMarshal.GetReference(s))` plus `s.Length`.
2. Vary the shape so each argument register is covered: `(int,int,int,int)`;
   a span starting in `r0`, `r1` or `r2`; a `null` object versus a real one.
3. Compare the result with a direct C# call. Also compare inlined with not
   inlined callees, and first call with second, to separate the call-site code
   from trampoline and patching effects.

This isolated the Thumb-2 `arm_branch_long` bug (#756, `r1 = r0 >> 16`) in
three deploys. Use marker values whose corruption you can recognise (see the
general skill).

### Diagnostic lines that are not the app's

`SOCKDIAG af=... fd=...` and `CONNDIAG fd=... ret=... errno=...` on the app
console come from shims in `apps/system/mono/mono_main.c` around
`SystemNative_Socket`/`SystemNative_Connect` (on main since 2ccc725edd9; #758
tracks removing them). One pair is printed per socket. They make a handy socket
counter: `ret=0` on every `CONNDIAG`, and the same fd coming back each cycle,
mean sockets are being closed.

### Seeing the app's stdout/stderr

`meadow listen -p -t` shows `stdout>`, `stderr>` and `info>` lines. This needs
`CONFIG_HCOM_MONO_STDERR_STDOUT=y`, which creates `/dev/monostdout` and
`/dev/monostderr`. Without it the boot log says `open /dev/monostdout failed:
errno=2` and the app's output goes nowhere. Two redirect bugs were fixed on
2026-10-14 (both FIFOs dup2'd onto fd 1; POLLHUP spin when the FIFO had no
writer). If labels look swapped or stderr is missing, suspect the redirect, not
the CLI.

## Deploying a managed app with the Meadow 3.0 CLI `(measured, 2026-10-15)`

How a Meadow.Core app reaches the board on a 3.0 (.NET 11 Mono) OS, and the
traps found on the way. Project Lab v3 (CCM2 = `F7CoreComputeV2`).

### Which CLI, and which path it takes

- The installed `meadow` tool may be 2.x (2.5.1 here), which only has the old
  netstandard deploy. The 3.0 CLI lives in Meadow.CLI's `meadow-3.0` branch;
  build it and run it as `dotnet <worktree>/Source/Meadow.CLI/bin/Release/meadow.dll`
  (a wrapper script avoids zsh not word-splitting a `$VAR` command). Both share
  the same route setting.
- The 3.0 CLI takes its **3.x path only when the device reports OS >= 2.999**
  (`MeadowVersion.IsV3OrLater`, `V3Preview = 2.999`); below that it silently
  uses the 2.x linker path. Check `meadow device info` first.
- The 3.x `app run`: disable the runtime, `dotnet publish -c Release
  --self-contained -r linux-arm` with injected MSBuild targets that swap in the
  Meadow BCL and trim (`BuildManager.MeadowTrimmingTargets`), deploy, enable the
  runtime, listen. `app.build.yaml` `NoLink` lists only affect the 2.x path; the
  3.x targets root every non-BCL assembly anyway.

### Where the managed assemblies come from

The CLI takes `meadow_assemblies` from its **firmware store**,
`~/Library/Application Support/WildernessLabs/Firmware/<version>/`. It uses the
newest package with the **same major.minor** as the device and a version
**<= the device's**. A package folder needs at least one of the OS images
(`Meadow.OS.bin`, `Meadow.OS.Update.bin`, `Meadow.OS.Runtime.bin`) to be listed,
plus `meadow_assemblies/`. For a locally built OS, create a folder named after
its exact version with those files and the locally built assemblies
(`build-managed-assemblies.sh` in Rebase); `meadow firmware list` should show it.

### What the deploy does to the board's files

- **It deletes every file in `/meadow0` that is not in the publish output**, so
  an app must ship its `meadow.config.yaml` (`<None Include=...
  CopyToOutputDirectory="PreserveNewest">`) or lose it. Measured deletions:
  `meadow.log`, `meadow.error`, `unittest.config.yaml`, the staged
  `Meadow.OS.Runtime.bin` (harmless: the runtime is in its own QSPI partition)
  and `dns.conf` (harmless: the OS recreates it on reset, per the maintainer).
- Unchanged files are skipped by CRC, so a redeploy only sends what changed.
- Size: a trimmed Blinky is 67 DLLs / 7.6 MB (CoreLib trimmed to 2.6 MB); the
  untrimmed reference closure was 113 files / 61 MB, more than the ~61 MB file
  system. Do not hand-deploy untrimmed framework DLLs.
- Mixing trimmed and untrimmed assemblies on the board is unsafe; when
  deploying by hand, delete DLLs that are not in the new set (as the CLI does).

### App-level traps

- `CreateDigitalOutputPort` throws `PortInUseException: Pin is defined as System
  Reserved` if `meadow.config.yaml` lists the pin under `ReservedPins` (this
  board had `C6;C7` for COM6, which are the Project Lab v3 RGB LED's red and
  green pins). Check the config before blaming the GPIO driver.
- Project Lab v3 RGB LED: D09 = PC6 (red), D10 = PC7 (green), D11 = PC9 (blue)
  (maintainer, from the schematic; matches the Meadow.Core `F7CoreCompute`
  pinout).
- Meadow.Core logs `Could not find a part of the path '/etc/resolv.conf'` at
  start-up; not fatal.
- One `app run` failed with only "Publish failed. Build output:" and nothing
  after it, and did not reproduce. The CLI's `dotnet clean` step logs only to
  `Debug`, so a clean failure shows no text. Re-run the same `dotnet publish`
  by hand to see real errors.

### Confirming GPIO at the pins, without halting

With OpenOCD running, sample GPIO registers over the telnet port while the app
runs; no halt needed. GPIOC: `MODER` at `0x40020800` (2 bits per pin: 01 =
output), `ODR` at `0x40020814` (1 bit per pin):

```
(printf 'mdw 0x40020800\n'; for i in 1 2 3 4 5 6 7 8; do printf 'mdw 0x40020814\n'; sleep 0.45; done; printf 'exit\n') | nc localhost 4444
```

Measured for Blinky: PC6/PC7/PC9 as outputs, and ODR bits 6/7/9 stepping
R -> G -> B -> off. That separates "the app thinks it is toggling" from "the pin
toggles", and is independent of whether anyone is watching the LED.

## Cell modem (BG95-M3) over PPP `(measured, 2026-10-26)`

Cell support is built only when `CONFIG_MEADOW_CELL_INCLUDE_IN_BUILD=y`. It was
added on the Rebase `BuildCellCode` branch; the plan and issue list are in
`OS/CellPlan.md`. The pieces:
- the kernel powers the modem on at boot (`hcom_nx_config_turn_on_the_cell_module`);
- hcom starts a `CellPPPD` thread (`apps/system/hcom/cell/hcom_pppd.c`);
- that thread runs NuttX `netutils/pppd`, which runs `chat` scripts on the modem
  UART, then PPP over `/dev/tun` (`ppp0`).

**pppd only starts if Mono started OK.** "Mono is disabled" on the host output
means no cell attempt at all. Once, `meadow runtime enable` reported ENABLED
after a deploy and the next boot still said Mono was disabled. Running it again
worked. Check for `Meadow successfully started MONO` before debugging cell.

### Bench setup and config

- **Modem:** BG95-M3 click board on **mikroBUS 1** = **COM1** = USART1 =
  `/dev/ttyS0`, enable (PWRKEY) pin **A3**, with a small antenna. No SIM was
  available on 2026-10-26.
- **`meadow.config.yaml`:** `Network: DefaultInterface: Cell`, plus
  `ReservedPins: ...;B15;B14;A3` (the COM1 pins and the enable pin). The
  interface name is `Cell` (`MEADOW_IFT_CELL_NAME`).
- **`/meadow0/cell.config.yaml`:**
  ```yaml
  Settings:
      APN: internet
      Module: BG95M3
      Interface: COM1
      EnablePin: A3
  ```
  Other optional keys: `User`, `Password`, `Operator`, `Mode`, `Timeout` (the
  chat timeout, default 30) and `ScanMode`, which is deprecated.
- **COM1 is shared with the debug/trace output.** It must be silenced for cell:
  **remove the whole `InternalDebug` section** from `meadow.config.yaml`. Legacy
  was designed to work this way. Verified by holding `stlink_vcp` open across a
  reset:
  - with the section: 8191 bytes of boot output;
  - without it: 0 bytes in 60 s, and the CDC port dropping and re-enumerating
    proved the board did reboot.
- **Back up `meadow.config.yaml` before changing it, and restore it after**
  (`meadow file read /meadow0/meadow.config.yaml <local>`; the bare name timed
  out). With `DefaultInterface: Cell` the board does not use WiFi.
  `wifi.config.yaml` is one-shot and gets deleted, so never print it.

### Watching it

- **Host output** (`meadow listen -t`) carries the `HCOM_CELL_DEBUG_LOGS`
  messages:
  - `Cell connect script failed, retrying... Script:`, then the script,
    `Response:` and the modem's captured output;
  - on link-up, `Connection established successfully! IP address ...` and the
    DNS servers.

  The `ESPCP_CELL_*` events (connecting, connected, retry exceeded) go to
  managed code, not to the host.
- **The kernel syslog** (power-on, cell settings) is in the RAM log; read it
  over SWD.
- **A sniffer on the modem UART is the decisive instrument** when the chat
  script fails. The user can wire a USB-serial adapter (`uart_sniffer1`, `uart_sniffer2` roles)
  onto the modem lines. Use it **receive-only**, 115200 8N1:
  - **Never connect the adapter's TX while the board is attached:** the board
    already drives the modem's RX.
  - **One channel sees one direction.** Ask which line it is on. The board TX
    (modem RX) shows the AT commands and the board's PPP frames. The modem TX
    shows the echo, `OK`, the IMEI, `CONNECT` and `RDY`/`APP RDY`.
  - **Capture passively with host timestamps.** Open the port, flush, then read
    `(t, bytes)` chunks and write them with `%r`, so `\r`, `\n` and binary PPP
    bytes stay visible. The board retries every 30-60 s, so 150-300 s covers
    two or three attempts without a reset.
  - **To test the wiring:** just listen. `AT+CFUN=1,1` appears within about a
    minute. To identify the modem (`ATI` -> `Quectel`, `BG95-M3`, revision,
    `OK`), hold the board in reset or remove its TX link first, then connect
    the adapter's TX and a common ground.
- **Reading PPP bytes:** frames are `0x7E`-delimited and use `}` escapes.
  `~\xff}#\xc0!` is LCP (`ff 03 c0 21`); `~\x80!` is IPCP (`80 21`).
  Configure-Requests repeating every 6 s mean the peer is not answering.

### Traps found

- **The console termios breaks the modem link.** `/dev/ttyS0` is the NuttX
  console, which defaults to `ECHO | ICANON | ISIG`, `ICRNL` and
  `OPOST | ONLCR` (`drivers/serial/serial.c`). The sniffer showed the board
  sending the modem's output straight back to it: `AT+CFUN=1,1`, `OK`, `RDY`,
  each `\r` coming back as `\r\n`. The echo happens when chat reads the data
  after a `PAUSE`.
  - The echoed `AT+CFUN=1,1` reset the modem a second time.
  - Line mode left chat with only `AT+CFUN=1,1`, so `OK` was never matched.
  - The `Response:` showing just the echoed command is the signature.
  - Fix: `hcom_pppd_set_tty_raw()` (`cfmakeraw` + `tcsetattr`) before
    `pppd()`. Legacy never hit this because it had no console
    (`# CONFIG_DEV_CONSOLE is not set`).
- **BG95 power-on pulses PWRKEY on every boot**, without checking the reset
  reason (the EG21 path does). An SWD or soft reset leaves the modem powered,
  so each reset can toggle it off and on. Keep this in mind when the modem
  goes silent, but it was not the cause of the echo failure (see Wrong turns).
- **The disconnect script is malformed** (`AT+CFUN=1,1 PAUSE 10 OK "" ATZ OK`):
  chat pairs are expect/send, so it waits 30 s for the modem to *say*
  `AT+CFUN=1,1`. That is the 30 s gap after `+++` / `ATE1` on every reconnect.

### What a healthy no-SIM attempt looks like on the board TX

The connect script ran end to end with the raw-mode fix:

```
+++                       (ppp_reconnect)
ATE1
  ... 30 s (disconnect script timeout, above)
AT+CFUN=1,1               then PAUSE 15
AT+CMEE=2 / AT+GSN / AT+CGDCONT=1,"IP","<apn>" / AT+QCSQ / AT+CSQ / AT+COPS=0   (3 s apart)
ATD*99#                   modem answers CONNECT
~\xff}#\xc0!...~          LCP, then IPCP Configure-Requests every 6 s
  ... no IPCP reply without a SIM -> timeout after about 30 s -> +++ again
```

The modem side, captured with a second adapter on the modem TX line (two ports
read in one loop against one clock), shows:
- `OK` to every command, the IMEI to `AT+GSN`, and `CONNECT 150000000`;
- LCP: the modem's Configure-Request offers CHAP-MD5 (option `03 05 c2 23 05`).
  The board sends Configure-Reject (code 4) for it and Configure-Ack (code 2)
  for the rest, and the modem re-requests without CHAP;
- the board's IPCP Configure-Request;
- **without a SIM, the modem then sends LCP Terminate-Request (code 5) and
  `NO CARRIER`**. That is normal: it cannot activate a PDP context. It also
  sends a harmless Discard-Request (code 11).

Decode LCP codes by un-escaping `}x` -> `x ^ 0x20`; the code byte follows
`ff 03 c0 21`. Two adapters that both report identical byte streams are on the
same wire.

### Syslog for cell debugging

COM1 belongs to the modem, so read the syslog from the second USB CDC port; see "The second USB
CDC port: syslog or NSH" under "Console and ports". Check the boot banner for `Mono:Enabled`
first, because pppd only starts with Mono, and see the runtime-enable trap there.

What the syslog showed during cell work: the kernel's cell config parsing, the PWRKEY pulse,
pppd start, and every `hcom_pppd.c` syslog line. Meanwhile the COM1 board TX carried only modem
traffic.

### Exercising the managed cell API `(measured, 2026-10-26)`

`ICellNetworkAdapter` (`Device.NetworkAdapters.Primary<ICellNetworkAdapter>()`)
offers `FetchGnssAtCmdsOutput(types, timeoutSeconds)`, `GetSignalQuality(t)` and
`ScanForAvailableNetworks(t)`. The timeouts are counted in **seconds**, although
one doc comment says milliseconds. How a call works:
- it calls `meadow_cell_change_state(bit)`, then hcom posts an AT script and sets
  `CELL_AT_CMD | CELL_PAUSED`;
- pppd runs the script inside its next `ppp_reconnect`;
- the ESPCP `NetworkAtCmdEvent` sets the managed state back to Resumed.

So the latency is "time to the next reconnect" plus the script time. GNSS took
207 s: about 57 s of waiting, then about 150 s of script, which includes
`PAUSE 120`.

To run a check, build a one-off app: a `Library`, `AssemblyName` `App`,
`net10.0`, referencing `Meadow.F7.csproj` from the `meadow-3.0` checkout. Deploy
it with `meadow app run <dir>`, which keeps listening afterwards.
- **Ship the current `meadow.config.yaml` and `cell.config.yaml` in the app**
  (`<None ... CopyToPublishDirectory="Always">`), because the deploy deletes
  every other file in `/meadow0`.
- Print tagged lines (`GNSSCHECK ...`), and run a background heartbeat thread so
  that a blocked call can be told apart from a dead runtime.
- Sniff both UART directions at the same time: the modem's answer decides
  whether a bad result is the board's fault.

What it showed:
- **GNSS:** without a GNSS antenna the script runs and the modem answers, but
  the NMEA is empty (`$GPGGA,,,,,,0`, `$GPRMC,,V`). With a GPS antenna on the
  BG95 (2026-10-26) the same 209 s run got a 3D fix: `$GPGGA` quality 1 with 9
  satellites, `$GPGSA` mode 3 with HDOP 0.8, and `$GPRMC` status `A`. The NMEA
  contains the bench's location, so keep coordinates out of PRs and notes.
- **Signal quality:** the -9999 result was the modem's own `+CSQ: 99,99`,
  because the one-shot script runs straight after a hang-up. Reproduced twice.
- **Syslog quirk:** the first `ppp_reconnect` logs "Cell network has been
  disconnected, error: 2" about 1 s after pppd starts, before any connection.
  Every reconnect then logs "error: 3" (timeout) from the malformed disconnect
  script.

With a SIM, expect IPCP to finish, `ppp0` to get an address and the
`Connection established` host message. Authentication has to come from
`AT+CGAUTH` in the connect script, because the board rejects CHAP.

## Wrong turns

- **Cell, 2026-10-26: "the BG95 PWRKEY toggle turned the modem off" was the
  first explanation for chat getting only `AT+CFUN=1,1` back.** A second reset,
  which should have toggled it back on, gave exactly the same result, which
  ruled it out as the main cause. The sniffer then showed the console echo
  described in "Cell modem" above. Lesson: the captured `Response:` is the
  modem's output **after the tty layer**, so a tty-mode problem looks like a
  modem problem until you see the wire.

- **Turning on `CONFIG_DEBUG_FEATURES` alone used to hang the board**, silently,
  between two `showprogress` characters. The cause is recorded in a comment in
  `stm32_boot.c` and is worth knowing before enabling debug output anywhere
  early: `stm32_boardinitialize()` runs before `nx_start()` has initialised
  `g_readytorun`, and the RAMLOG syslog backend calls `sched_lock()`, which
  dereferences a NULL `this_task()` that early -- **a silent HardFault**, because
  `DEBUGASSERT` is compiled out unless `CONFIG_DEBUG_ASSERTIONS` is also set.
  Two lessons generalise: do not call `syslog()` from board init, and
  `CONFIG_DEBUG_FEATURES` without `CONFIG_DEBUG_ASSERTIONS` removes the
  assertion that would have told you why.

- **An earlier version of this file said `VTOR` is `0x00200000` on this board.**
  It is not a constant; see the table above. Recorded here because the claim
  read as verified and was a single snapshot under a since-replaced image.

- **An earlier version also called the `0x08040000` table "the user image"**,
  reasoning from `flash.cfg`'s offsets. It is the NuttX kernel. The offsets in a
  flash script describe what that script **would** write, not what is on the
  board -- here the script short-circuits on `Meadow.OS.bin` and never reaches
  them. Compare image bytes against flash rather than reading offsets out of a
  script.

- **`nuttx/nuttx_user.bin` is a 3 GB file of zeros on this bench** `(measured)`.
  A broken build artefact. Nothing flashed it, because `flash.cfg` exits after
  writing `Meadow.OS.bin`, but a change to that ordering would try to.

- **Looking for `nuttx/nuttx` and concluding the kernel was not built.** The
  artefact is `nuttx/nuttx.elf`. `Documentation/Debugging.md` names the wrong
  file; `gdb/.gdbinit` names the right one. Believe the `.gdbinit`.

- **A rebuild does not match the flashed image, even at the same version**
  `(measured)`. Rebuilding produced `1.99.0.19595`, the same string the board
  prints, while the kernel vector entries moved from `081661c9`/`081661d3` to
  `081661c1`/`081661cb` -- eight bytes. Symbols from a fresh build applied to
  firmware flashed from an older one give plausible, wrong answers. **Reflash,
  or symbolicate against the exact image that is on the board.**

- **`meadow file read` needs an absolute local path** `(measured)`. Given a
  relative one it fails with `The value cannot be an empty string. (Parameter
  'path')` and then times out, which reads like a device communication problem
  and is not one. It also disables the runtime on the way in.

- **OpenOCD prints only the last command of a compound `-c`** `(measured)`.
  `-c 'mdw A; mdw B'` shows B and silently drops A, so a register you believe
  you read may never have been reported. One `-c` per `mdw`.

- **`Documentation/Debugging.md` has the flash offsets wrong** `(measured)`.
  Its prose says `nuttx.bin` at `0x08040000` and `nuttx_user.bin` at
  `0x08100000`; the code block immediately below it says `0x08000000`, and
  `flash.cfg` itself uses `0x08000000` and `0x08040000`. The file contradicts
  itself within four lines. Trust `flash.cfg`, and read any offset in that
  document as unverified.

- **Reaching for `openocd` by name gets the ESP32 fork.** It is the only one on
  `PATH`. It will start, and for an STM32 target it is not what the project
  builds and tests against. Use `$MEADOW_OPENOCD_DIR/src/openocd`.

- **Port numbers from a previous session do not survive a board swap.** Two
  boards on this bench enumerated as `usbserial-100/101` and
  `usbserial-21112100/21112101`. A script with yesterday's number in it fails
  in a way that looks like a dead probe.

- **An impossible halt address on the STM32 is probably erratum 3092511**, not
  memory corruption -- see `stm32-family.md`. OpenOCD reports the workaround on
  every connect, and the message is easy to scroll past.

- **A GDB batch script that ends without `monitor resume` leaves the STM32
  halted** `(measured, 2026-10-14)`. GDB reports `[Inferior 1 (Remote target)
  detached]`, but with this OpenOCD the target stays halted at the last stop.
  Every later `meadow` command then times out, which looks exactly like a USB or
  CLI fault. End scripts with `delete`, `monitor resume`, `detach`. Check with
  `targets` on the OpenOCD telnet port (4444), which also takes `halt`,
  `dump_image` and `resume` while GDB is not attached.

- **After a long halt the Meadow USB CDC can stay unusable after resuming**
  `(measured)`. The CPU was running and hcom was up (RAM log), but the CLI timed
  out until the board was reset. Reset after any long halt before blaming the
  firmware.

- **The serial port names changed mid-session** `(measured, 2026-10-14)`. After
  the USB hub layout changed, `usbserial-21112101`/`usbmodem21112203` became
  `usbserial-21101` and `usbmodem21203`. The Meadow CDC then appeared as
  `/dev/cu.usbmodem<serial>1` (named by serial number), and `meadow port
  list` reported no devices although the port worked; `meadow config route
  <port>` fixed the CLI. Re-list `/dev/cu.*` after any unexplained timeout.
  Unresolved: in that layout `usbmodem21203` gave 0 bytes during a reset loop
  that was logging to the RAM buffer, so it was not confirmed as the syslog
  port. Use the RAM log over SWD (section above) when in doubt.

- **Symbolicating a reset with the wrong build's ELF was 2 bytes off**
  `(measured)`. The breakpoint at `meadow_os_reset_board` reported a PC in the
  tail of the previous function. Same lesson as the rebuild entry above, but at
  the same version string again: flash the build whose ELF you are using.

- **Do not trust a committed test binary's source** `(measured)`. Legacy's
  `hello-world/out/Meadow.dll` returns 42 but never calls `Console`, unlike its
  `Program.cs`, which cost a hunt for missing console output. Check a .NET
  binary's member references with `arm-none-eabi-strings -a` before debugging
  its output.

Add to this section from real sessions. It is the part of the file that cannot
be reconstructed afterwards.
