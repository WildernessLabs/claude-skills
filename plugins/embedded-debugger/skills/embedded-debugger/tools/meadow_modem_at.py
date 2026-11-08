#
# Talk to the cell modem directly through a USB-serial adapter wired to its UART.
# See targets/meadow.md, "Talking to the modem directly": take the Meadow off the line first.
# Ports are the 2026-10-28 bench wiring; ask the user and edit TX/RX if they differ.
#
# usage: modem_at.py <logfile> <item>...   item = "AT..." command, "@cld" CMUX close-down, "+N" wait N s
import serial, sys, time
RX = "/dev/cu.usbserial-559B0124241"   # monitors the modem TX (replies)
TX = "/dev/cu.usbserial-559C0002201"   # TX drives the modem RX; RX sees the modem RX line
def fcs(b):
    f = 0xFF
    for x in b:
        f ^= x
        for _ in range(8):
            f = (f >> 1) ^ 0xE0 if f & 1 else f >> 1
    return 0xFF - f
def frame(addr, ctl, info):
    hdr = bytes([addr, ctl, (len(info) << 1) | 1])
    return b"\xf9" + hdr + info + bytes([fcs(hdr)]) + b"\xf9"
CLD = frame(0x03, 0xEF, b"\xc3\x01")
import os
s = serial.Serial(TX, 115200, timeout=0)
class _N:
    def read(self, n): return b""
    def reset_input_buffer(self): pass
m = serial.Serial(RX, 115200, timeout=0) if os.path.exists(RX) else _N()
time.sleep(0.2); m.reset_input_buffer(); s.reset_input_buffer()
log = open(sys.argv[1], "a"); t0 = time.time()
def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        a = m.read(4096); b = s.read(4096)
        if a: log.write("%7.2f MODEM %r\n" % (time.time() - t0, a)); print("  <", a.decode("latin1").replace("\r", "").strip())
        if b: log.write("%7.2f TXPORT %r\n" % (time.time() - t0, b)); print("  559C<", b.decode("latin1").replace("\r", "|").strip())
        if not (a or b): time.sleep(0.01)
for item in sys.argv[2:]:
    if item.startswith("+"): pump(float(item[1:])); continue
    data = CLD if item == "@cld" else (item + "\r").encode()
    print(">", item); log.write("%7.2f SEND  %r\n" % (time.time() - t0, data)); s.write(data); pump(2)
pump(0.5); log.close()
