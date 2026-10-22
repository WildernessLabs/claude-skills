#!/usr/bin/env python3
"""Summarise a Meadow ESP32 (MeadowComms) console capture.

Collapses each Logging::DumpMessage block into one line, decodes the payloads
it knows, and reports the +++ control markers. See
../targets/meadow-esp32-trace.md for what the output means.

Tables mirror OS/Meadow-ESP32 components/F7Services/Messaging/SharedEnums.hpp
at develop 57cc1ad5. If a decode looks wrong, re-check that file first.

Usage: meadow_esp32_log.py CAPTURE [--markers] [--all]
  --markers  print every control marker in sequence (default: counts only)
  --all      also print trace lines that are not part of a message dump
"""
import re
import struct
import sys

MESSAGE_TYPES = {0x00: "Ack", 0x01: "Nak", 0x02: "Reset", 0x04: "Event", 0x10: "Response",
                 0x20: "Transport", 0x40: "Request", 0x80: "Data"}
INTERFACES = {0: "None", 1: "WiFi", 2: "Bluetooth", 3: "MeshNetwork", 4: "System", 5: "Transport",
              6: "WiredEthernet"}
SYSTEM = ["GetConfiguration", "SetConfigurationItem", "DeepSleep", "GetBatteryChargeLevel", "ErrorEvent",
          "StartHeapTrace", "StopHeapTrace", "FileSystemFormat", "FileSystemListFiles", "FileSystemWriteFile",
          "FileSystemReadFile", "FileSystemDeleteFile", "OsException", "LoggingConfiguration", "LogMessage",
          "GetCoreDumpInformation", "GetCoreDumpFragment", "EraseCoreDump"]
WIFI = ["StartWiFiInterface", "StopWiFiInterface", "ConnectToAccessPoint", "ConnectToDefaultAccessPoint",
        "ClearDefaultAccessPoint", "DisconnectFromAccessPoint", "GetAccessPoints", "SetAntenna", "Socket",
        "Connect", "Write", "SetSockOpt", "GetSockOpt", "Read", "Close", "SendTo", "RecvFrom", "Poll",
        "InterruptPollResponse", "Send", "Bind", "Listen", "Accept", "Ioctl", "GetSockName", "GetPeerName",
        "FreeAddrInfo", "GetAddrInfo", "RecvMsg", "Shutdown", "SendMsg", "Dup2", "AddRef", "SockCaps",
        "StartWiFiInterfaceEvent", "StopWiFiInterfaceEvent", "NetworkConnectedEvent",
        "NetworkDisconnectedEvent", "NtpUpdateEvent", "ErrorEvent", "StartAccessPoint", "StopAccessPoint",
        "AccessPointStartedEvent", "AccessPointStoppedEvent", "NodeConnectedEvent", "NodeDisconnectedEvent",
        "NetworkConnectionRetryCountExceededEvent", "NetworkConnectingEvent", "NetworkGotIpEvent",
        "MaximumConnectionsReachedEvent"]
BLUETOOTH = ["Start", "Stop", "GetHandles", "ServerDataSet", "ClientWriteRequestEvent", "BluetoothStartingEvent",
             "BluetoothStartedEvent", "BluetoothStoppingEvent", "BluetoothStoppedEvent", "ClientConnectedEvent",
             "ClientDisconnectedEvent"]
TRANSPORT = ["ResponseReady", "SendResponse", "KillNuttxThread", "ResetEsp32"]
FUNCTIONS = {1: WIFI, 2: BLUETOOTH, 4: SYSTEM, 5: TRANSPORT}
CONFIGURATION_ITEMS = ["MaximumMessageQueueLength", "AutomaticallyStartNetwork", "AutomaticallyReconnect",
                       "MaximumRetryCount", "DeviceName", "DefaultApAndPassword", "NtpServer", "GetTimeAtStartup",
                       "UseDhcp", "StaticIpAddress", "DnsServer", "DefaultGateway", "Antenna", "BoardMacAddress",
                       "SoftApMacAddress", "SubNetMask", "BluetoothMacAddress", "CountryCode"]
STATUS = ["CompletedOk", "CrcError", "Restart", "Failure", "InvalidInterface", "QueueError", "Timeout",
          "InvalidPacket", "InvalidHeader", "UnexpectedData", "MissingEndOfFrameMarker", "HeaderBodyFieldMismatch",
          "WiFiAlreadyStarted", "InvalidWiFiCredentials", "WiFiDisconnected", "CannotStartNetworkInterface",
          "CannotConnectToAccessPoint", "DefaultAccessPointNotConfigured", "InvalidAntennaData",
          "InvalidAntennaValue", "InvalidIp", "NoMessagesWaiting", "CoprocessorNotResponding", "EspWiFiNotStarted",
          "EspOutOfMemory", "EspWiFiInvalidSsid", "AccessPointNotFound", "BeaconTimeout", "AuthenticationFailed",
          "AssociationFailed", "HandshakeTimeout", "ConnectionFailed", "ApTsfReset", "UnmappedErrorCode",
          "UnknownConfigurationItem", "CannotStartAccessPoint", "DhcpConfigurationError", "AccessPointNotStarted",
          "AccessPointAlreadyStarted", "NotImplemented", "FileNotFound", "ThreadPoolIsFull",
          "UnexpectedCoprocessorRestart", "InvalidConfigurationFile", "InvalidWiFiConfigurationFile",
          "InvalidCellConfigurationFile", "NetworkDeadlock", "BatteryLevelCannotBeRead", "CannotStopBluetooth",
          "BluetoothAlreadyStarted", "EspResetUnknown", "EspResetPowerOn", "EspResetExternalGpio",
          "EspResetSoftware", "EspResetPanic", "EspResetInterruptWatchdog", "EspResetTaskWatchdog",
          "EspResetOtherWatchdog", "EspResetDeepSleep", "EspResetBrownout", "EspResetSdio", "InvalidRequest",
          "InvalidCountryCode"]
RESET_REASONS = ["Unknown", "PowerOn", "ExternalPin", "Software", "PANIC", "InterruptWatchdog", "TaskWatchdog",
                 "OtherWatchdog", "DeepSleep", "Brownout", "SDIO"]
DIRECTION = {"RequestHandlerBase": "STM32->ESP32", "TransportProviderBase": "ESP32->STM32 queued",
             "SPI": "ESP32->STM32 wire"}

LOG = re.compile(r"^([IWED]) \((\d+)\) ([^:]+): ?(.*)$")
MARKER = re.compile(r"\+\+\+([A-Z]+)(?:,(-?\d+))?")
HEX = re.compile(r"^(?:[0-9a-f]{2} ?)+$")


def name(table, value):
    return table[value] if 0 <= value < len(table) else "?"


def decode_payload(interface, function, message_type, data):
    """Return a short decode of payloads worth recognising, or ''."""
    try:
        if interface == 4 and function == 1 and len(data) >= 8:
            item, length = struct.unpack_from("<II", data)
            value = data[8:8 + length]
            text = value.rstrip(b"\0").decode("ascii") if value and all(32 <= b < 127 for b in value.rstrip(b"\0")) else value.hex(" ")
            return f"{name(CONFIGURATION_ITEMS, item)}({item}) = {text!r}"
        if interface == 4 and function == 15 and message_type == 0x10 and len(data) >= 9:
            valid, size, partition = struct.unpack_from("<BII", data)
            return f"IsValid={valid} CoreDumpSize={size} PartitionSize={partition}"
        if interface == 4 and function == 16:
            if message_type == 0x40 and len(data) >= 8:
                offset, size = struct.unpack_from("<II", data)
                return f"Offset={offset} Size={size} (end {offset + size})"
            if message_type == 0x10 and len(data) >= 4:
                (size,) = struct.unpack_from("<I", data)
                note = "" if len(data) == 4 + size else f" (payload {len(data)} != 4+Size: pre-#501 encoder, last 4 bytes unwritten)"
                return f"Size={size}{note}"
    except (struct.error, UnicodeDecodeError):
        pass
    return ""


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    show_markers = "--markers" in argv
    show_all = "--all" in argv
    raw = open(argv[1], "rb").read().decode("utf-8", errors="replace").replace("\r", "")
    markers, messages, other = [], [], []
    current = None

    def finish():
        nonlocal current
        if current:
            messages.append(current)
        current = None

    for line_number, line in enumerate(raw.split("\n"), 1):
        for m in MARKER.finditer(line):
            markers.append((line_number, m.group(1), m.group(2)))
        line = MARKER.sub("", line).strip()
        #  Host timestamp added by a capture script, and the NuttX ESP Log relay prefix.
        line = re.sub(r"^\d+\.\d+ +", "", line)
        line = re.sub(r"^\[ *[\d.]+\] \[ *\d+\] ESP Log: ", "", line)
        m = LOG.match(line)
        if not m:
            continue
        _, stamp, tag, text = m.groups()
        tag = tag.strip()
        if text.startswith("Message type:"):
            finish()
            current = {"stamp": int(stamp), "tag": tag, "line": line_number, "payload": bytearray(),
                       "type": int(re.search(r"0x([0-9a-f]+)", text).group(1), 16)}
            continue
        if current and tag == current["tag"]:
            f = re.match(r"(ESP32 Interface|Message ID|Packet offset \(length\)|Function to be executed|"
                         r"Status code|Payload length): (.*)", text)
            if f:
                key, value = f.groups()
                if key == "ESP32 Interface":
                    current["interface"] = int(value.split()[0], 16)
                elif key == "Message ID":
                    current["id"] = int(value, 16)
                elif key.startswith("Packet"):
                    current["packet"] = value.replace(" bytes", "")
                elif key.startswith("Function"):
                    current["function"] = int(value.split()[0], 16)
                elif key == "Status code":
                    current["status"] = int(value.split()[0], 16)
                elif key == "Payload length":
                    current["length"] = int(value)
                continue
            if HEX.match(text) and len(current["payload"]) < current.get("length", 0):
                current["payload"] += bytes.fromhex(text)
                continue
            if text == "Payload has not been set.":
                current["unset"] = True
                continue
        finish()
        other.append((line_number, stamp, tag, text))
    finish()

    counts = {}
    for _, kind, _ in markers:
        counts[kind] = counts.get(kind, 0) + 1
    print("== Control markers: " + (", ".join(f"{k} x{v}" for k, v in counts.items()) or "none"))
    for line_number, kind, value in markers:
        if kind == "RST":
            reason = name(RESET_REASONS, int(value)) if value else "?"
            print(f"   line {line_number}: +++RST,{value} ({reason})")
        if kind == "BSY":
            print(f"   line {line_number}: +++BSY - a message was DROPPED")
    rdy = [n for n, k, _ in markers if k == "RDY"]
    if len(rdy) > 1:
        print(f"   +++RDY seen {len(rdy)} times - SPI not armed for ~{2 * (len(rdy) - 1)} s")
    if show_markers:
        print("   sequence: " + " ".join(k + (f",{v}" if v else "") for _, k, v in markers))

    print(f"== Messages: {len(messages)}")
    for msg in messages:
        interface = msg.get("interface", -1)
        function = msg.get("function", -1)
        table = FUNCTIONS.get(interface, [])
        status = msg.get("status", -1)
        ident = msg.get("id", 0)
        origin = "S" if ident & 0x80000000 else "E"
        payload = bytes(msg["payload"])
        short = " SHORT" if len(payload) < msg.get("length", 0) else ""
        decode = decode_payload(interface, function, msg["type"], payload)
        print(f"{msg['stamp']:>8} L{msg['line']:<5} {DIRECTION.get(msg['tag'], msg['tag']):<20} "
              f"{MESSAGE_TYPES.get(msg['type'], hex(msg['type'])):<8} id=0x{ident:08x}{origin} "
              f"{INTERFACES.get(interface, interface)}.{name(table, function)}({function}) "
              f"status={name(STATUS, status)}({status}) pkt={msg.get('packet', '?')} "
              f"len={msg.get('length', 0)}{short}" + (f"  {decode}" if decode else "")
              + ("  PAYLOAD NOT SET" if msg.get("unset") else ""))

    errors = [o for o in other if re.search(r"error|fail|drop|cannot|invalid|timeout", o[3], re.I)]
    print(f"== Other trace lines: {len(other)}, of which {len(errors)} look like errors")
    for line_number, stamp, tag, text in (other if show_all else errors):
        print(f"{stamp:>8} L{line_number:<5} {tag}: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
