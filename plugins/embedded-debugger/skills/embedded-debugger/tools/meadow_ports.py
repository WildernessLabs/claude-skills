#!/usr/bin/env python3
"""Find the Meadow bench serial ports by USB identity, not by name.

macOS names USB serial nodes from the USB location (and sometimes the
device serial number), so the names change when a hub is added, removed or
re-enumerates. The vendor/product IDs do not. This prints one line per role:

    meadow_cdc=/dev/cu.usbmodem<serial>1        Meadow USB CDC (HCOM, Meadow CLI)
    meadow_cdc2=/dev/cu.usbmodem<serial>3       second Meadow CDC (--dualcdc): syslog, or NSH with --nsh
    stlink_vcp=/dev/cu.usbmodem21203            ST-Link V3 virtual COM port
    espprog_jtag=/dev/cu.usbserial-21100        ESP-Prog channel A (JTAG, OpenOCD)
    esp_console=/dev/cu.usbserial-21101         ESP-Prog channel B (ESP32 console)
    uart_sniffer1=/dev/cu.usbserial-559B0124241 WCH CH343 adapter tapped onto a board UART (RX only)
    uart_sniffer2=/dev/cu.usbserial-559C0002201 a second one, for the other direction

Usage:
    meadow_ports.py            list every role found
    meadow_ports.py ROLE       print just that role's port (for $(...)), exit 1 if absent

Needs pyserial (the same dependency as meadow_capture.py).
"""

import re
import subprocess
import sys

from serial.tools import list_ports

MEADOW_VID = 0x2E6A          # Wilderness Labs ("Meadow F7 Micro")
STLINK_VID = 0x0483          # STMicroelectronics
STLINK_PIDS = {0x374E, 0x374F, 0x3753, 0x3754, 0x374B, 0x3752}  # V3 / V2-1 VCP variants
FTDI_VID = 0x0403
FTDI_DUAL_PID = 0x6010       # FT2232H "Dual RS232-HS" = ESP-Prog
WCH_VID = 0x1A86             # WCH; 0x55D4 = CH343/CH9102 "USB Single Serial",
WCH_SNIFFER_PIDS = {0x55D4}  # the user's sniffer on the cell modem UART (2026-10-26)


def find_roles():
    roles = {}
    ftdi = []
    meadow = []
    for port in list_ports.comports():
        device = port.device
        if not device.startswith("/dev/cu."):
            continue
        if port.vid == MEADOW_VID:
            meadow.append(device)
        elif (port.vid == STLINK_VID) and (port.pid in STLINK_PIDS):
            roles["stlink_vcp"] = device
        elif (port.vid == FTDI_VID) and (port.pid == FTDI_DUAL_PID):
            ftdi.append((port.location or "", device))
        elif (port.vid == WCH_VID) and (port.pid in WCH_SNIFFER_PIDS):
            # Up to one adapter per UART direction; which line each is on is
            # wiring, not USB identity, so ask (or compare captures).
            n = 1
            while ("uart_sniffer%d" % n) in roles:
                n += 1
            roles["uart_sniffer%d" % n] = device
    # A --dualcdc build exposes two CDC nodes from one board: <serial>1 is HCOM and
    # <serial>3 is the second channel (the syslog without --nsh, NSH with it).
    meadow.sort()
    if meadow:
        roles["meadow_cdc"] = meadow[0]
    if len(meadow) > 1:
        roles["meadow_cdc2"] = meadow[1]
    # The STM32 ROM bootloader (DFU) is not a serial port, so pyserial does not
    # list it.  It shows up as "STM32  BOOTLOADER", 0483:DF11, in the USB tree.
    if "meadow_cdc" not in roles:
        dfu = find_dfu()
        if dfu is not None:
            roles["meadow_dfu"] = dfu
    # The FT2232H exposes two nodes on one location; macOS numbers interface A
    # before interface B (measured: ...00 = A/JTAG, ...01 = B/console).  With
    # more than one ESP-Prog attached this pairing is ambiguous, so say so.
    ftdi.sort()
    if len(ftdi) == 2:
        roles["espprog_jtag"] = ftdi[0][1]
        roles["esp_console"] = ftdi[1][1]
    elif len(ftdi) > 2:
        roles["espprog_ambiguous"] = " ".join(device for _, device in ftdi)
    return roles


def find_dfu():
    try:
        tree = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w0"],
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # Each device's properties follow its node line; look for the bootloader
    # product name and the locationID that belongs to the same node.
    for block in re.split(r"\n\s*[|+ ]*\+-o ", tree):
        if "STM32  BOOTLOADER" in block:
            m = re.search(r'"locationID" = (\d+)', block)
            return "DFU (STM32 BOOTLOADER, locationID %#010x)" % int(m.group(1)) if m else "DFU (STM32 BOOTLOADER)"
    return None


DESCRIPTIONS = {
    "meadow_cdc": "Meadow USB CDC (HCOM, Meadow CLI); absent until the OS runs hcom",
    "meadow_cdc2": "second Meadow CDC (--dualcdc): syslog without --nsh, NSH with it",
    "meadow_dfu": "board is in the STM32 ROM bootloader (DFU), not running Meadow.OS",
    "stlink_vcp": "ST-Link virtual COM port",
    "espprog_jtag": "ESP-Prog channel A (JTAG; claimed by OpenOCD)",
    "esp_console": "ESP-Prog channel B (ESP32 console and control markers)",
    "espprog_ambiguous": "more than one ESP-Prog attached; pick by location",
    "uart_sniffer1": "WCH CH343 adapter on a board UART line; usually RX only, may also have TX wired to the cell modem; ask the user",
    "uart_sniffer2": "second WCH CH343 adapter; one per UART direction, or one full duplex to the modem; ask the user",
}


def main():
    roles = find_roles()
    if len(sys.argv) > 1:
        port = roles.get(sys.argv[1])
        if port is None:
            sys.stderr.write("%s: not found\n" % sys.argv[1])
            return 1
        print(port)
        return 0
    if not roles:
        print("no Meadow bench ports found")
        return 1
    for role, port in sorted(roles.items()):
        print("%s=%-36s %s" % (role, port, DESCRIPTIONS.get(role, "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
