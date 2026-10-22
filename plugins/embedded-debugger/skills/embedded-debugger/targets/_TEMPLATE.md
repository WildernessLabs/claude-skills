# Target profile: <CHIP> (<vendor>, <arch>, <interrupt controller>)

Copy this file, answer the questions by **measuring**, and delete anything that
turned out not to apply. A profile written from datasheet recall is worse than
no profile: it reads as verified and is not.

Mark each item with how you know it. `(measured)` for something you observed on
this hardware, `(assumed)` for anything else - and treat `(assumed)` as a thing
to check the first time it matters.

---

## Connection

- Probe and server command line (ST-LINK/J-Link/built-in USB-JTAG; OpenOCD cfg,
  probe-rs chip name, pyOCD target).
- GDB binary for the architecture; how to connect (some hosts need
  `localhost:PORT` rather than `:PORT`).
- Does the server assume an RTOS? A mismatched setting spams warnings and slows
  every halt - disable it.
- Do the debug link and the console share a physical device? If so, what has to
  be released before flashing?

## Console

- Which physical port carries application output, and at what baud?
- Is it the same device as the debug link, or separate? Separate means you can
  hold it open across a reset - see `../SKILL.md` section 1b.
- How is the board reset non-interactively, and through which port?
- Is there a **polled** low-level output path that works when interrupts are
  dead? Name it - this is the one to report through.

## Memory map and breakpoints

- Which regions execute from flash (need `hbreak`) and which from RAM
  (`break` works)? A software breakpoint in XIP flash usually fails silently.
- How many hardware triggers exist, and are breakpoints and watchpoints drawn
  from the same pool?

## Boot and reset

- Does a bootloader copy RAM sections from flash on every reset? If so, any
  software breakpoint placed there before the copy is erased while GDB still
  reports it armed - find a post-copy address for stage 1.
- What does `monitor reset halt` actually reset? Does it clear triggers?
- Do triggers survive a killed GDB session, and what clears them?

## Mode and privilege

- Which register reports execution mode, and is it meaningful at the point you
  sample it? Many report the handler's mode, not the interrupted code's.
- Does it survive a halt-and-resume, or does the debug architecture latch the
  mode to resume into? If the latter, readings across a resume are not
  evidence.

## Interrupt controller

- How does the cause/status register encode the active source?
- Does the vendor SDK's source numbering match the RTOS's? Check, do not
  assume; identify a live interrupt at runtime instead of from a table.
- Is any of the controller's state readable over the debug bus, or must it be
  sampled by code running on the target?
- Does the controller carry state that the RTOS's context switch must preserve
  (priority/level masks, nesting depth)? This is where the expensive bugs live.

## Failure signatures

- What does an assertion do - reboot, or hang? Where does it print?
- What does a fault handler do, and does its output reach the console you are
  watching? A handler that spins silently is indistinguishable from the hang
  you are chasing.

## Wrong turns

Record each one that cost real time, with what would have caught it. This
section is the most valuable part of the file for the next session, and the
only part that cannot be reconstructed later.
