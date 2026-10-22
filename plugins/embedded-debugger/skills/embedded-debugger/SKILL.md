---
name: embedded-debugger
description: Drive GDB against a physical MCU over JTAG/SWD (OpenOCD, probe-rs, pyOCD) and capture its serial console directly to diagnose firmware faults - hangs, crashes, privilege and interrupt bugs - by running batch scripts and opening the port yourself instead of asking the user to run commands or read a terminal back to you.
---

# Embedded Debugger

Use this when diagnosing firmware on real hardware through a debug probe:
hangs, resets, faults, privilege problems, interrupt storms, memory corruption.

Assume the user has started the GDB server and flashed the board. Your job is
to **drive the debugger yourself** and report conclusions.

## 1. Prime directive: take over the debugger

Do not hand the user a list of GDB commands to type and paste back. Every
round trip costs minutes, loses context, and invites transcription errors.
Instead write a GDB batch script and run it.

```bash
riscv32-esp-elf-gdb -q -batch -x session.gdb firmware.elf > out.log 2>&1
```

Ask the user only for things you cannot do: power-cycling, moving a jumper,
plugging something in. **Serial output is not on that list** - open the port
yourself, see section 1b. Restarting the GDB server is usually yours too.

**Check whether the debug port is free before connecting** - only one client
may attach:

```bash
lsof -nP -iTCP:3333
```

If the user has an interactive GDB attached, ask them to quit it rather than
killing their process.

## 1b. Take over the serial console too

The application's own output is usually the *better* instrument - it survives
conditions that wedge the debugger, it costs no round trips, and it does not
perturb timing the way a breakpoint does. Do not ask the user to read a
terminal to you and do not ask them to paste a log. Open the port.

```python
import serial, subprocess, sys, time
ser = serial.Serial("/dev/cu.usbserial-XXXX", 115200, timeout=0.2)
time.sleep(0.3); ser.reset_input_buffer()      # drop the previous run
subprocess.run([...reset the board...], capture_output=True)
buf = bytearray(); t0 = time.time()
while time.time() - t0 < seconds:
    d = ser.read(4096)
    if d: buf += d
ser.close(); open(out, "wb").write(buf)
```

Four things that make or break this:

- **Open the port before triggering the reset**, or you lose the boot banner
  and whatever the fault printed on the way down.
- **Flush the input buffer after opening.** A stale buffer read as fresh output
  is a classic wrong turn, and it is very convincing.
- **The reset and the console are often different ports.** On boards with a
  separate programmer/USB-JTAG interface, reset through that one while holding
  the console port open.
- **A terminal program holding the port makes the open fail** (`Resource busy`
  on macOS). That is one of the few things worth asking the user to do: close
  it.

Decode as `utf-8` with `errors="replace"` and strip `\r` - a resetting board
emits partial lines and ROM output that is not always valid UTF-8, and `tr`
will fail with `Illegal byte sequence` on it.

### Make the firmware report on a path that cannot fail

If you can change the firmware, do not report through the interrupt-driven
console: it is exactly what dies in interrupt, privilege and scheduler bugs, so
a wedged core is indistinguishable from a dead console. Print from the kernel
through the **polled** low-level path instead (`up_putc`/`syslog` in NuttX, the
ROM print routine in ESP-IDF), which busy-waits on the TX FIFO and works
whether or not interrupts are alive.

Emit **a line before each step as well as after it**. A step that announces
itself and never reports has hung *inside that step* - a different fact from
the console dying, and one you cannot otherwise tell apart. That distinction is
what converts "it stops somewhere in here" into a one-line bisection.

Related: an absent line does not always mean the code did not run. On a
buffered UART driver nothing drains the TX buffer unless something pushes it,
so a task that writes and then blocks can produce no output at all.

## 2. Measurement discipline

This is the part that actually determines whether a session succeeds. Debugging
hardware punishes confident inference more than almost any other work.

**Absence of evidence is not evidence.** A breakpoint that did not fire, a
console with no output, a log that stops - none of these mean what they appear
to. Before drawing any conclusion from silence, prove the instrument works:
set the breakpoint unconditionally and show that it fires at all.

**Verify the instrument before trusting the reading.** If a register or
expression is central to a conclusion, first demonstrate that it can report a
different value. A register that reads `3` at every address you sample may
simply be incapable of reading anything else.

**One decisive measurement beats three plausible patches.** When you can name
two hypotheses, look for the single reading that separates them, and take that
reading before changing anything.

**Never change code to test a hypothesis you have not measured.** Editing
sources to fix an inferred bug produces a new system whose behaviour you also
do not understand, and can introduce a second fault that masks the first.

**Record retractions in place.** When a conclusion turns out to be wrong, write
the correction into the notes next to the original claim. A long investigation
accumulates dead theories; anyone reading later (including you, after a
context reset) must be able to tell which conclusions still stand.

**Check `$pc` at every stop** before reading frame-relative memory. If you are
not where you think you are, every subsequent number is fiction.

**Read every time in one clock.** Local `date` output, GitHub and CI timestamps
(UTC, `...Z`) and device logs are often in different time zones. Once, a
two-minute-old build was reported as hung for an hour: local BST was compared
with UTC. Convert both to the same zone, or take elapsed times from one source,
before calling anything slow or stuck. The device's own clock is a further
source: embedded logs often count from reset rather than give dates, and the
wall clock may start at a build or default time and jump when network time
arrives. Check the target profile for what each of its timestamps means.

**Prove an operating mode actually changed before comparing results.** Two runs
that differ only in a setting (JIT or interpreter, debug or release, a config
flag) can still share the mode, because the setting was not applied, came from
a stale file, or was copied from elsewhere. Add a cheap reading that differs
sharply between the modes, such as a timed hot loop, and check it in both runs.
Identical results from two "different" runs prove nothing until then.

**Rule out a fast reset loop before calling anything a hang.** A board that
faults, reboots and reruns the same start-up every few seconds looks hung from
outside: the host tool stops printing, and every PC sample lands in busy,
plausible code. Once, 30 samples "all in the JIT compiler" read as one endless
compile for over 15 minutes. Mono was really compiling the same start-up
methods after each of 1400+ reboots. Samples taken in *different* functions do
not prove progress. Read a reset counter, a boot banner or an uptime value, and
take it twice.

**Use test values that show how they were corrupted.** Zero, null and small
round numbers are bad probes: many different bugs turn them into the same
wrong answer. A null and `0x1234` both arrived as `0`. Only a real pointer
(`0xc03a53c0` arriving as `0xc03a`) showed that the value was being shifted
right by 16. That pointed to the exact instruction. Pick values whose
corruption you can recognise, and probe each argument position separately.

**Dead stack still holds evidence.** Memory just below the current SP is
whatever the last callee or trampoline left there, and frames do not always
overwrite all of it. A register save area left behind by a call path you
could not catch with a breakpoint can still show what a register held at the
call. Map out which slots the current frame wrote before trusting the rest.

## 3. Running sessions

### Batch script pattern

```gdb
set confirm off
set pagination off
set height 0
set width 0
target extended-remote localhost:3333
monitor reset halt
delete
hbreak *0xADDR
continue
printf "STAGE1 pc=%#x\n", $pc
```

Prefix every diagnostic line with a unique tag (`STAGE1`, `RET`, `STOP`) so it
can be filtered out of the noise.

### Long runs go in the background

Foreground commands are killed by tool timeouts, and on many targets a killed
GDB **leaks hardware breakpoint triggers** that survive `monitor reset halt`.
Run anything that might exceed a minute in the background and poll the output
file.

Guard the GDB process itself so it cannot hang forever (macOS has no
`timeout(1)`):

```bash
perl -e 'alarm 540; exec @ARGV' gdb -q -batch -x s.gdb fw.elf > out.log 2>&1
```

### Scripts must always resume the target

A plain GDB batch script stops at the first error, for example `frame function
foo` when no such frame exists. Every command after it, including your final
`monitor resume`, is skipped, and the target stays halted. A session that
`alarm` kills is worse: Python's buffered output is lost and breakpoints can
stay armed. For anything that can fail midway, write the session in GDB's
Python and resume in `finally`:

```python
import gdb
gdb.execute("target extended-remote localhost:3333")
try:
    gdb.execute("monitor halt", to_string=True)
    ...                                # samples, reads, breakpoints
finally:
    gdb.execute("delete")
    gdb.execute("monitor resume", to_string=True)
    gdb.execute("detach")
```

Wrap per-sample reads that may fail (walking frames, dereferencing) in their
own `try`. After any killed session, run a short cleanup script (halt, delete,
resume) and check the halt bit before going on.

### Filtering output

Users often have a `.gdbinit` dashboard that renders registers, assembly and
source at every stop, turning a short run into hundreds of KB of ANSI. Strip
and filter:

```bash
perl -pe 's/\e\[[0-9;]*[a-zA-Z]//g' out.log | grep -E "^(STAGE|RET|STOP|DONE)"
```

### Loops in scripts

GDB supports `while`, `if/else`, `set $var`, and `printf`. Use a bounded
counter and an early-exit flag so a script cannot run away:

```gdb
set $n = 0
set $done = 0
while ($n < 80 && $done == 0)
  continue
  if ($pc == $EXPECTED)
    printf "RET %d ...\n", $n
  end
  set $n = $n + 1
end
```

### Cost model

Each breakpoint round trip over JTAG costs roughly **4-5 seconds**. A
breakpoint on a hot path (an exception return, a timer ISR) fires constantly
and turns a sweep into many minutes. Prefer a targeted hardware breakpoint on
the exact instruction of interest over a broad sweep with a condition.

## 4. Universal traps

- **Breakpoint conditions using registers are unreliable.** In a GDB condition,
  register access is frame-relative and need not equal the raw register,
  especially inside assembly with no frame info. The same expression can read
  one value in the condition and another at the stop. Prefer unconditional
  breakpoints plus manual reads, or filter inside the script after stopping.
- **Flash needs hardware breakpoints.** `break` writes a trap instruction into
  memory; in XIP flash the write silently does nothing and the breakpoint never
  fires. Use `hbreak` for anything executing from flash.
- **Hardware triggers are a scarce, leaky resource.** Typically 2-8 per hart.
  `Cannot insert hardware breakpoint: Remote failure reply: 0E` means they are
  exhausted, often by a previously killed session. `monitor reset halt` does
  not always clear them; restarting the GDB server does.
- **Triggers are per-hart on multi-core parts.** Confirm `info threads` shows
  the core actually running your code.
- **Resets erase breakpoints in two different ways.** A reset clears hardware
  triggers, and on many SoCs a ROM/second-stage bootloader **copies RAM
  sections from flash on every boot**, erasing software breakpoints placed in
  those regions. GDB reports both as armed. See the two-stage pattern below.
- **Console silence proves nothing.** Firmware logging and application output
  often share one link; output can sit buffered, and a blocking driver can stop
  delivery while execution continues.
- **A serial terminal's scrollback mixes runs.** Ask the user to clear it
  before each run, or you will read a stale crash dump as a fresh one.
- **A branch into the middle of an instruction runs a different program.** With
  variable-length encodings (Thumb-2, RISC-V C, Xtensa), a target that is not an
  instruction boundary in the disassembly decodes the following halfwords as
  something else. Inline literals already disassemble as junk, so junk alone
  means nothing. Check generated code's branch targets against the instruction
  list instead. Once, `b.w` skipped a literal but landed 2 bytes late, inside
  `orr.w ip, ip, #1`, whose second halfword runs as `lsrs r1, r0, #16`. That
  silently corrupted an argument on every call made from that code layout.

### The two-stage arming pattern

Where a bootloader reloads RAM, nothing may be armed before the first reset.

```gdb
# Stage 1: reset, then a HARDWARE breakpoint in flash, after the RAM copy
monitor reset halt
delete
hbreak *0xFLASH_ADDR_AFTER_BOOT
continue

# Stage 2: RAM is now loaded and stable - software breakpoints will stick
delete
break *0xRAM_ADDR
continue
```

## 5. Adapting to a new target

Before the first real measurement, establish these. Guessing any of them
produces false negatives that can cost hours.

1. **Memory map** - which regions execute from flash (need `hbreak`) and which
   from RAM (`break` works)?
2. **Boot behaviour** - does a bootloader copy RAM sections from flash on
   every reset? If so, find a post-copy address for stage 1.
3. **Trigger budget** - how many hardware breakpoints/watchpoints exist, and
   are they shared with watchpoints?
4. **Reset semantics** - what does `monitor reset halt` actually reset? Does it
   clear triggers?
5. **Privilege/mode reporting** - which register reports execution mode, and is
   it meaningful at the point you are sampling?
6. **Interrupt controller** - how does the cause register encode the active
   source, and does the vendor SDK's numbering match the RTOS's?
7. **RTOS awareness** - a mismatched setting spams warnings and slows every
   halt. Disable it if the RTOS is not the one the server expects.

8. **Console** - which port carries application output, is it the same device
   as the debug link, and is there a polled output path that survives dead
   interrupts?

Record the answers in a target profile under `targets/`, starting from
`targets/_TEMPLATE.md`, so the next session does not rediscover them. Note how
you know each answer: a profile written from recall reads as verified and is
not.

Where several parts share a trait, put it in a **family** profile and keep the
chip file for what is genuinely chip-specific - see `targets/esp32-family.md`
and `targets/esp32p4.md`.

## 6. Identifying a live interrupt

Static tables lie. Vendor SDKs and RTOSes frequently maintain **separate,
conflicting interrupt-source numberings**, and reading the wrong header will
misidentify a peripheral confidently.

Identify the source at runtime instead: break in the dispatcher and read the
handler pointer and its argument, then resolve them to symbols:

```gdb
info symbol <handler-expression>
p *(struct <adapter-type> *)$a0
```

Before blaming an interrupt for a livelock, check whether the associated
peripheral is actually making progress - a transmit buffer whose tail advances
is working normally, not storming.

## 7. Workflow

1. **Establish the fault.** Halt, `bt`, mode register, cause register. Sample
   twice, seconds apart: identical state means stuck, differing means running.
2. **Bracket it.** Find one point where behaviour is correct and one where it
   is wrong, then bisect between them with targeted breakpoints.
3. **Take the decisive reading.** Name the two candidate explanations and the
   single measurement separating them.
4. **Only then change code**, one thing at a time, and re-measure.
5. **Write it down** as you go - measurements, retractions, addresses, and the
   commands that produced them.

## 8. Handoff notes

Long hardware investigations outlive a session. Maintain a notes file with:

- what is **measured** versus **inferred**, kept separate
- retractions written next to the claims they overturn
- the exact next measurement, with the commands to run it
- environment: toolchain, branch, uncommitted files, config flags that matter
- target-specific traps discovered
- **a warning that addresses change on every build**, plus the commands to
  re-derive them

Keep reusable GDB helpers in a file in the repository, not in a scratch
directory that disappears with the session.

## Files

- `templates/session.gdb` - starting point with `connect` / `stage1` / `frm`
  helpers and a bounded hunt loop
- `targets/_TEMPLATE.md` - the questions a new target profile must answer;
  copy it when starting one
- `targets/esp32-family.md` - Espressif ESP32 line, Xtensa and RISC-V: toolchain,
  flashing vs the debug link, breakpoint types, the bootloader erasing RAM
  breakpoints, the two console ports, interrupt-numbering traps
- `targets/esp32p4.md` - ESP32-P4 and its CLIC specifically; read the family
  file first
- `targets/esp32c6.md` - ESP32-C6: PLIC not CLIC, so the P4 file does not
  apply; resetting through USB-JTAG parks the chip in download mode, the
  bridge takes reads but not bulk writes, flash offset is 0x0 not 0x2000
- `targets/esp32-pico-d4-espprog.md` - ESP32-PICO-D4 over ESP-Prog: the FTDI
  channel pair and which one is the console, the core dump partition as an
  after-the-fact fault detector, driving a Unity test menu
- `targets/stm32-family.md` - STM32 on Arm Cortex-M: ST-Link and the stm32<fam>x
  cfgs, reading trigger counts rather than recalling them, VTOR and the RAM-copy
  trap, NVIC and the off-by-16, CFSR/HFSR after a fault
- `targets/meadow.md` - Wilderness Labs Meadow F7, **two MCUs on one board**:
  STM32F777ZIT6 under NuttX plus an ESP32-PICO-D4 coprocessor. Three OpenOCD
  builds that are not interchangeable, halting one MCU while the other runs,
  and the SPI link between them; also Mono/.NET runtime bring-up on the Rebase
  OS: reading the RAM syslog over SWD, recognising and breaking hcom's
  reset-on-exit loop, the breakpoints that classify a runtime failure, and
  decoding the NuttX hard-fault context; deploying a managed app with the 3.0
  Meadow CLI (version gate, firmware store, what the deploy deletes) and
  checking GPIO at the pins over SWD; proving JIT versus interpreter, running
  Mono's mini test suite with the test runner and its baseline, thread names
  and NSH `ps` on the second CDC port, and how ESP32 socket errors reach an app;
  a late fault that looks like a hang (reset code `0x14`), breaking a
  fault-driven reset loop, naming the method the JIT is compiling, probing JIT
  calling conventions from managed code, and Mono mixed mode (`--interp=jit=`).
  Read the two family files first
- `targets/meadow-esp32-trace.md` - decoding the Meadow ESP32 console: the
  `+++RST/RDY/SIR/MW/BSY` control markers (the console *is* the control link),
  why logging is silent until the STM32 configures it, `DumpMessage` blocks,
  enum tables, the wire header, the core dump request/response payloads and a
  healthy retrieval to compare with, capturing a boot loop, and verifying an
  ESP32 fix on the board (including why silence after an ESP32-only flash
  proves nothing)
- `tools/meadow_capture.py` - holds the ESP32 console and the STM32 syslog open
  together, stamps each line with host time, and reports byte counts
- `tools/meadow_ports.py` - finds the Meadow bench ports (Meadow CDC, ST-Link
  VCP, ESP-Prog JTAG and console) by USB vendor/product ID, because the node
  names change when the USB layout does
- `tools/meadow_esp32_log.py` - collapses a Meadow ESP32 capture into one line
  per message with decoded payloads, and summarises the control markers
