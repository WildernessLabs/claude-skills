# Target profile: ESP32-C6 (Espressif, RISC-V, PLIC)

**Read `esp32-family.md` first** -- the two console ports, the ROM bootloader
erasing RAM breakpoints, trigger leakage and the SDK/RTOS interrupt-numbering
trap all apply. This file is only what is specific to the C6.

Derived from a NuttX flat-build bring-up on a **Waveshare ESP32-C6 DevKit N8**,
2026-09-14, used as a *non-CLIC control* while fixing a P4 CLIC bug.

## The C6 is PLIC, not CLIC -- none of `esp32p4.md` applies

Per the vendored HAL's `soc_caps.h` the C6 and H2 are **PLIC** parts. The whole
CLIC section of `esp32p4.md` -- `mcause` carrying `mpp`/`mpie`/`mpil`, the
memory-mapped interrupt threshold, `mintstatus` at CSR 0x346, the NLBITS level
encoding -- is **absent here**. `mie`/`mip` behave normally.

That makes the C6 the right board for proving a change to shared RISC-V code is
a no-op off the CLIC path. `(measured)`

## Two ports, one chip -- and they are not equivalent

The DevKit exposes both interfaces; both report the **same MAC**, so identity
is not how you tell them apart:

| Port (macOS, this board) | What it is | Use |
|---|---|---|
| `/dev/cu.usbmodem1401` | built-in USB-Serial/JTAG | **flashing**, OpenOCD |
| `/dev/cu.usbmodem<serial>` | CH343-class UART bridge | **console** (UART0), DTR/RTS reset |

Note the bridge enumerates as `usbmodem*`, **not** `usbserial*` -- the family
file's naming heuristic does not hold on this board. Identify by behaviour, not
by name. `(measured)`

NuttX `esp32c6-devkitc:nsh` sets `CONFIG_UART0_SERIAL_CONSOLE=y` at 115200, and
that reaches the bridge port. `(measured)`

## Resetting through USB-JTAG parks the chip in download mode

The single biggest time sink here. esptool's `--before default_reset` on the
**built-in USB-Serial/JTAG** interface is how it forces download mode, and on
this part `--after hard_reset` did **not** get it back out. Every reset landed
at:

```
rst:0x15 (USB_UART_HPSYS),boot:0x4 (DOWNLOAD(USB/UART0/SDIO_FEI_FEO))
waiting for download
```

This is indistinguishable from "my image does not boot" if you are not watching
the ROM banner, and it survives repeated resets, so it reads as a hard failure.
It is not -- the BOOT button is not involved and nothing is wrong with the image.

**Reset for a normal boot by driving the bridge's DTR/RTS yourself**, holding
IO9 high so the chip does not strap into download:

```python
ser = serial.Serial(BRIDGE_PORT, 115200, timeout=0.2)
ser.setDTR(False)     # IO9 high  -> NOT download mode
ser.setRTS(True)      # EN low    -> reset asserted
time.sleep(0.15); ser.reset_input_buffer()
ser.setRTS(False)     # EN high   -> release, normal boot
```

That booted first time after many failed USB-JTAG resets. `(measured)`

The family file's capture recipe -- reset via esptool on the USB-JTAG port while
holding the console open -- **works on the P4 but not here.** Use the DTR/RTS
sequence above on the C6.

## Flashing: bridge reads fine but cannot take bulk writes

On this board the bridge port connects and answers small ROM-loader commands
(`chip_id`, `flash_id`, `get_security_info`) perfectly, yet **every bulk write
fails**:

- with the stub: `Failed to write to target RAM (result was 0107: Checksum error)`
- `--no-stub --no-compress`: `Failed to write to target Flash after seq 0
  (result was 0105: The format of the received message is invalid)`

Unchanged across 921600 / 460800 / 115200. Reads working while writes fail
points at the link, not the chip -- confirm the chip is not locked
(`get_security_info` showed Secure Boot and Flash Encryption both **disabled**)
before chasing it further. `(measured)`

**What worked:** put the board in download mode, then flash over USB-JTAG with
`--before no_reset` so esptool does not disturb the state it is already in:

```bash
esptool.py -c esp32c6 -p /dev/cu.usbmodem1401 \
  --before no_reset --after hard_reset -b 460800 write_flash -fs 8MB 0x0 nuttx.bin
```

`--before no_reset` is the flag to reach for whenever the board is *already*
where you need it and esptool's own reset is what keeps knocking it out.

## Flash offset is 0x0, not 0x2000

Under NuttX `CONFIG_ESPRESSIF_SIMPLE_BOOT`, `tools/espressif/Config.mk` sets
`APP_OFFSET := 0x2000` **only** for `ARCH_CHIP_ESP32P4`; every other chip,
including the C6, uses `0x0000`. Copying a P4 flash command and keeping
`0x2000` produces a board that flashes cleanly and never boots. `(measured)`

`flash_id` reports the real part -- this DevKit N8 is **8 MB**, while
`esp32c6-devkitc:nsh` defaults to `CONFIG_ESPRESSIF_FLASH_4M`. That mismatch is
harmless (the image just does not use the upper half); `-fs 8MB` on the
`write_flash` corrects the header without a rebuild. `(measured)`

## Wrong turns

1. **Blamed the board for being stuck in download mode.** Asked the user to
   check the BOOT button and swap cables, when the cause was my own reset
   method -- I was resetting through the interface whose reset *means* "enter
   download mode". The ROM banner said `boot:0x4 DOWNLOAD` on every attempt and
   I read it as a symptom rather than as the answer.
2. **Read "no console output" as a dead image.** Both ports were silent because
   the chip was sitting in the download-mode prompt, not because the flash had
   failed -- the flash had in fact verified. Opening the console *across* a
   reset and reading the ROM banner is what distinguished the two, and it
   should have been the first move, not the fourth.
