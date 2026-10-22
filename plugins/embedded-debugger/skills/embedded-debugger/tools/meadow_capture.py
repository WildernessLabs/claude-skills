#!/usr/bin/env python3
"""Capture the Meadow ESP32 console and the STM32 NuttX syslog at the same time.

Each port gets its own thread. Every complete line is written to
<out>.<name>.log with the host time since start, so the two logs can be
lined up; the untouched bytes go to <out>.<name>.raw.

Usage: meadow_capture.py OUT SECONDS [--esp PORT] [--stm PORT]

  --esp  ESP-Prog console. Default: the highest-numbered /dev/cu.usbserial-*
         (channel B of the FT2232H pair). Confirm by behaviour.
  --stm  ST-Link VCP carrying the NuttX syslog. There is no reliable default,
         because other usbmodem devices enumerate too: list /dev/cu.usbmodem*
         and pick the one that prints "Meadow.OS initialization began".

Start it before triggering a reset, or the boot banner is lost. The byte counts
printed at the end matter: zero bytes on both ports means nothing ran, not that
something worked (see ../targets/meadow-esp32-trace.md).
"""
import glob
import sys
import threading
import time

import serial


def option(name, default=None):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    out, seconds = sys.argv[1], float(sys.argv[2])
    serial_ports = sorted(glob.glob("/dev/cu.usbserial-*"))
    ports = {"esp": option("--esp", serial_ports[-1] if serial_ports else None), "stm": option("--stm")}
    ports = {name: port for name, port in ports.items() if port}
    if not ports:
        print("No ports: give --esp and/or --stm.")
        return 2
    counts = {}
    start = time.time()

    def run(name, port):
        connection = serial.Serial(port, 115200, timeout=0.05)
        time.sleep(0.2)
        connection.reset_input_buffer()     # drop whatever the previous run left behind
        received = 0
        pending = b""
        with open(f"{out}.{name}.raw", "wb") as raw, open(f"{out}.{name}.log", "w") as stamped:
            while time.time() - start < seconds:
                data = connection.read(4096)
                if not data:
                    continue
                received += len(data)
                raw.write(data)
                pending += data
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    stamped.write(f"{time.time() - start:9.3f} {line.decode('utf-8', 'replace').rstrip(chr(13))}\n")
                stamped.flush()
            if pending:
                stamped.write(f"{time.time() - start:9.3f} {pending.decode('utf-8', 'replace')}\n")
        connection.close()
        counts[name] = received

    threads = [threading.Thread(target=run, args=item) for item in ports.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for name, port in ports.items():
        print(f"{name}: {port} {counts.get(name, 0)} bytes -> {out}.{name}.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
