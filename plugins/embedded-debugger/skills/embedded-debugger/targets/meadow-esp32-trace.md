# Reading the Meadow ESP32 console: control markers and message traces

Companion to `meadow.md`. That file says which port to open. This one says what
the ESP32 (MeadowComms, repository `OS/Meadow-ESP32`) writes to it, and how to
decode it.

> **Provenance.** `(repo)` = read from `OS/Meadow-ESP32` source on `develop` at
> `57cc1ad5` (2026-09-24). `(capture)` = seen in a capture from a Meadow F7
> board on 2026-09-24, built with
> `build.sh --build --debug=wifi,messages,system --tracemessages --fullclean`
> (app `2.5.7.364`, ESP-IDF v5.5.3). `(inferred)` = neither. Check it the first
> time it matters.
>
> Numeric values come from `components/F7Services/Messaging/SharedEnums.hpp`.
> If they look wrong, re-read that file. Do not trust this table over the
> source.

Decoder: `../tools/meadow_esp32_log.py <capture>` turns each multi-line message
dump into one line, decodes the payloads it recognises, and counts the control
markers. **Use it rather than reading a long capture by eye.**

---

## The console carries four kinds of traffic

In the order they appear after a reset:

1. **ROM and ESP-IDF boot log**: `rst:`, `load:`, `I (530) cpu_start: ...`.
   This is standard ESP-IDF output. See `esp32-pico-d4-espprog.md`.
2. **Control markers**: `+++RST,1`, `+++RDY`, `+++SIR`, `+++MW`, `+++BSY`.
   These are **not log lines. They are the control link.**
3. **Trace lines** from the MeadowComms components, `I (987) <Tag>: ...`.
4. **Hex dumps** of message payloads and raw SPI frames, under the same tags.

### The control link is the console `(repo)`

`TransportProviderBase::SendControlMessageToStm32()` uses a plain `printf` to
UART0, TX on `GPIO1`. The STM32 reads that same line on its UART5 (see the UART
table in `meadow.md`) and picks the `+++` markers out of the stream. So:

- Control markers and log lines share one wire and interleave freely. A marker
  can land in the middle of a log line. **This is normal, not corruption.**
- Turning logging up puts more bytes on the STM32's control channel. A
  heavily traced build changes the timing of the link it is tracing.
  `(inferred)`
- On the ESP-Prog console you see exactly what the STM32 sees.

## Control markers `(repo)`

| Marker | Meaning | Sent when |
|---|---|---|
| `+++RST,<n>` | ESP32 has booted. `<n>` = `esp_reset_reason()` | once, from the `SpiTransportProvider` constructor |
| `+++RDY` | ESP32 up, SPI slave **not yet armed** | end of that constructor, then **every 2 s** until the first SPI transaction is armed |
| `+++SIR` | SPI Interface Ready: a slave transaction is armed and waiting for the STM32 to clock it | every time `SpiPostSetupCallback` fires, once per armed transaction |
| `+++MW` | Message Waiting: a response or event is queued for the STM32 | each successful `QueueMessageForStm32` |
| `+++BSY` | outbound queue full. **A message was dropped** | when `QueueMessageForStm32` runs out of attempts |

`RST` is preceded by a newline so that it starts a fresh line even when it
follows a partial boot-log line.

**`<n>` in `+++RST` is the raw ESP-IDF `esp_reset_reason_t`**, not a Meadow
`StatusCodes` value:

| n | Reason | n | Reason |
|---|---|---|---|
| 0 | Unknown | 6 | Task watchdog |
| 1 | Power on | 7 | Other watchdog |
| 2 | External pin | 8 | Deep sleep |
| 3 | Software (`esp_restart`) | 9 | Brownout |
| **4** | **Panic**, so a core dump should now exist | 10 | SDIO |
| 5 | Interrupt watchdog | | |

`SharedEnums` repeats these as `StatusCodes::EspResetUnknown`=50 …
`EspResetSdio`=60. Seeing 50-60 in a `Status code:` line is the same
information mapped into the status space. It is not a new code.

### Reading the marker sequence

Healthy start `(capture)`: `+++RST,1`, `+++RDY` once, then a run of `+++SIR`
with `+++MW` among them. This is the sequence at the top of the 2026-09-24
capture.

- **`+++RDY` repeating every ~2 s**: the SPI slave never got a transaction
  armed. The STM32 has not started talking, or SPI setup failed. The code
  comment says 500 ms; the code (`_controlSignalDelay`) says 2000 ms `(repo)`.
- **Once `+++SIR` appears, `+++RDY` never appears again** in that boot. The
  signal switches permanently in the ISR `(repo)`.
- **`+++MW` with no `+++SIR` after it**: the ESP32 has a response queued and
  the STM32 is not collecting it. Look at the STM32 side. `(inferred)`
- **Any `+++BSY`** is a real fault: a response or event was thrown away, and
  the STM32 request waiting on it will time out. Look for
  `Cannot queue message after N attempt(s), dropping it.` just before it.
- **A second `+++RST` in one capture** means the ESP32 rebooted. Remember that
  resetting the STM32 also resets the ESP32 (`meadow.md`). Check the number:
  1 after an STM32 reset, 4 after an ESP32 panic.

## Why the log looks empty after `main_task:` `(repo)`

`Logging::Setup()`, the first call in `app_main`, does
`esp_log_level_set("*", ESP_LOG_NONE)` and reinstalls the UART0 driver. From
that point **every ESP_LOG tag is silent** until the STM32 sends a System
`LoggingConfiguration` request (function 13) naming components to enable.

So:

- The truncated `I (677) main_task:` at the end of the boot log `(capture)` is
  `main_task: Calling app_main()` cut off when the UART is reconfigured
  `(inferred)`. It is not a crash.
- MeadowComms' own `Version: ...` banner is logged at INFO after logging has
  been silenced, so it never appears. Its absence is expected.
- Anything the ESP32 logs **before** the STM32's `LoggingConfiguration`
  request is lost, **including errors**.

### Two gates, both needed

| Gate | Set by | Controls |
|---|---|---|
| Compile time | `build.sh --tracemessages` → `-DTRACE_MESSAGES=1` | whether `TRACE_MESSAGE`, `ERROR_MESSAGE` and `TRACE_HEX_BUFFER` compile at all. **Without it even `ERROR_MESSAGE` compiles to nothing** |
| Run time | `CoProcessor.LogComponents` in `meadow.config.yaml`, sent by the STM32 | which tags are raised to INFO |

Runtime component names, matched case-insensitively (`Logging::TurnLoggingOn`):

| Name in `LogComponents` | Tags it enables |
|---|---|
| `WiFi` | `WiFi` |
| `System` | `System`, `FileSystem`, `CoreDump` |
| `CoreDump` | `CoreDump` |
| `Bluetooth` | `Bluetooth` |
| `SPI` | `SPI`: the SPI state machine and **raw frame hex dumps** |
| `Thread` | `Thread`, `ThreadPool` |
| `Messages` | `RequestHandlerBase`, `TransportProviderBase`, `MessageDispatcher`: **the decoded message dumps** |

`RequestHandlerBase:` lines in a capture therefore mean `Messages` is in
`LogComponents`. `SPI:` lines mean `SPI` is.

**Turning it on for an app** `(per the maintainer, 2026-10-22)`. To watch the
ESP32's WiFi connection attempts, set `LogComponents: WiFi;Messages` (with
`LogDestination: UART`) under `CoProcessor:` in the app project's
`meadow.config.yaml`. Then put the file on the board, in either of two ways:

- write just that file to the board and restart the app;
- redeploy the app with `meadow app run`, which writes the project's copy.

The setting only produces output if the firmware was built with
`--tracemessages`: the compile-time gate above.

Capture the ESP-Prog console (channel B, `cu.usbserial-...1`) at the same time.
It shows the ESP32's own output whether or not the STM32 relays it as
`ESP Log:` lines.

**`--debug=<x>` does not turn on log tags.** It defines `WIFI_DEBUG`,
`SYSTEM_DEBUG` and so on, and those gate only a few extra blocks: poll-request
dumps in `PosixAPIHandlers`, a file listing in `FileSystem`, and `#pragma`
messages. `--debug=messages` defines `MESSAGES_DEBUG`, and nothing reads that
except a `#pragma` in `AppMain.cpp` `(repo)`. What you see is decided by
`--tracemessages` together with `LogComponents`.

`build.sh` matches subsystem names **in lower case only**. `--debug=WIFI`, as
written in the project `CLAUDE.md`, exits with `Unknown debug subsystem` `(repo)`.

## Decoded message dump: `Logging::DumpMessage` `(repo)`

Each message produces six lines, or eight if it has a payload, all under one
tag and one timestamp:

```
I (987) RequestHandlerBase: Message type: 0x40 (Request)
I (987) RequestHandlerBase: ESP32 Interface: 0x04 (System)
I (997) RequestHandlerBase: Message ID: 0x80000005
I (997) RequestHandlerBase: Packet offset (length): 0 (11) bytes
I (1007) RequestHandlerBase: Function to be executed: 0x00000001 (SetConfigurationItem)
I (1007) RequestHandlerBase: Status code: 0x00000000 (CompletedOk)
I (1017) RequestHandlerBase: Payload length: 11
I (1017) RequestHandlerBase: 11 00 00 00 03 00 00 00 47 42 00
```

**The tag says where the dump was taken, and so which direction:**

| Tag | Dumped in | Direction |
|---|---|---|
| `RequestHandlerBase` | `QueueEsp32Message`, as a complete request is queued to WiFi, System or BT | STM32 → ESP32 |
| `TransportProviderBase` | `QueueMessageForStm32`, followed by `+++MW` | ESP32 → STM32 |
| `SPI` | `ProcessTransportMessage`, after the line `Preparing to send message:` | ESP32 → STM32, as it is put on the wire |
| `WiFi` | `AccessPoint.cpp`, `WiFiRequestHandler.cpp` | WiFi internals |

Field notes:

- **Message type**: Ack `0x00`, Nak `0x01`, Reset `0x02`, Event `0x04`,
  Response `0x10`, Transport `0x20`, **Header `0x40` (printed `Request`)**, Data
  `0x80`.
- **Interface**: None 0, WiFi 1, Bluetooth 2, MeshNetwork 3, System 4,
  Transport 5, WiredEthernet 6.
- **Message ID, top bit**: set (`0x8xxxxxxx`) means the STM32 allocated it.
  Clear means the ESP32 did, which is what unsolicited ESP32 events carry.
  **A handler turns the request into its own response**, so a response has the
  same ID as its request. Use the ID to pair them.
- **Packet offset (length)**: the part of the payload in this SPI packet.
  Payloads over roughly 4 KB arrive as several packets, and the dump is taken
  only once the whole message has been reassembled.
- **Status code**: `CompletedOk` on the requests seen so far `(capture)`. It is
  the field that matters on responses.
- **Name lookup**: `Unknown` means the value is missing from `Mapping.cpp`.
  `Not implemented` means the interface has no function table. **Some enum
  values have no name.** For example System `ErrorEvent` 4 and `OsException`
  12, WiFi `StartWiFiInterface` 0, and several status codes such as
  `NotImplemented` 39 and `InvalidCountryCode` 62 all print `Unknown`. Go by
  the number.
- **Payload hex**: `ESP_LOG_BUFFER_HEX_LEVEL`, 16 bytes per line, each line
  with its own timestamp. **Multi-byte fields are little-endian.**

### Worked example (the dump above)

`11 00 00 00 | 03 00 00 00 | 47 42 00` is a `ConfigurationValue`: `Item`
u32 = 17 (`CountryCode`), `ValueLength` u32 = 3, `Value` = `"GB\0"`. The STM32
is pushing `Network.CountryCode: GB` from `meadow.config.yaml`.

## Function numbers `(repo)`

**System (interface 4):** 0 GetConfiguration, 1 SetConfigurationItem,
2 DeepSleep, 3 GetBatteryChargeLevel, 4 ErrorEvent, 5 StartHeapTrace,
6 StopHeapTrace, 7 FileSystemFormat, 8 FileSystemListFiles,
9 FileSystemWriteFile, 10 FileSystemReadFile, 11 FileSystemDeleteFile,
12 OsException, 13 LoggingConfiguration, 14 LogMessage,
**15 GetCoreDumpInformation, 16 GetCoreDumpFragment, 17 EraseCoreDump**.

**WiFi (interface 1):** 0 StartWiFiInterface, 1 StopWiFiInterface,
2 ConnectToAccessPoint, 3 ConnectToDefaultAccessPoint, 4 ClearDefaultAccessPoint,
5 DisconnectFromAccessPoint, 6 GetAccessPoints, 7 SetAntenna, 8 Socket,
9 Connect, 10 Write, 11 SetSockOpt, 12 GetSockOpt, 13 Read, 14 Close, 15 SendTo,
16 RecvFrom, **17 Poll**, 18 InterruptPollResponse, 19 Send, 20 Bind, 21 Listen,
22 Accept, 23 Ioctl, 24 GetSockName, 25 GetPeerName, 26 FreeAddrInfo,
27 GetAddrInfo, 28 RecvMsg, 29 Shutdown, 30 SendMsg, 31 Dup2, 32 AddRef,
33 SockCaps. Events: 34 StartWiFiInterfaceEvent, 35 StopWiFiInterfaceEvent,
36 NetworkConnectedEvent, 37 NetworkDisconnectedEvent, 38 NtpUpdateEvent,
39 ErrorEvent, 40 StartAccessPoint, 41 StopAccessPoint,
42 AccessPointStartedEvent, 43 AccessPointStoppedEvent, 44 NodeConnectedEvent,
45 NodeDisconnectedEvent, 46 NetworkConnectionRetryCountExceededEvent,
47 NetworkConnectingEvent, 48 NetworkGotIpEvent, 49 MaximumConnectionsReachedEvent.

Remember from `meadow.md` that a `Poll` (17) left without a response is by
design, not a hang.

**Bluetooth (interface 2):** 0 Start, 1 Stop, 2 GetHandles, 3 ServerDataSet,
4 ClientWriteRequestEvent, 5-8 Starting/Started/Stopping/StoppedEvent,
9 ClientConnectedEvent, 10 ClientDisconnectedEvent.

**Transport (interface 5):** 0 ResponseReady, 1 SendResponse, 2 KillNuttxThread,
3 ResetEsp32.

**Configuration items** (the first u32 of a SetConfigurationItem payload):
0 MaximumMessageQueueLength, 1 AutomaticallyStartNetwork, 2 AutomaticallyReconnect,
3 MaximumRetryCount, 4 DeviceName, 5 DefaultApAndPassword, 6 NtpServer,
7 GetTimeAtStartup, 8 UseDhcp, 9 StaticIpAddress, 10 DnsServer, 11 DefaultGateway,
12 Antenna, 13 BoardMacAddress, 14 SoftApMacAddress, 15 SubNetMask,
16 BluetoothMacAddress, 17 CountryCode.

**Status codes:** 0 CompletedOk, 1 CrcError, 2 Restart, 3 Failure,
4 InvalidInterface, 5 QueueError, 6 Timeout, 7 InvalidPacket, 8 InvalidHeader,
9 UnexpectedData, 10 MissingEndOfFrameMarker, 11 HeaderBodyFieldMismatch,
12 WiFiAlreadyStarted, 13 InvalidWiFiCredentials, 14 WiFiDisconnected,
15 CannotStartNetworkInterface, 16 CannotConnectToAccessPoint,
17 DefaultAccessPointNotConfigured, 18 InvalidAntennaData, 19 InvalidAntennaValue,
20 InvalidIp, 21 NoMessagesWaiting, 22 CoprocessorNotResponding,
23 EspWiFiNotStarted, 24 EspOutOfMemory, 25 EspWiFiInvalidSsid,
26 AccessPointNotFound, 27 BeaconTimeout, 28 AuthenticationFailed,
29 AssociationFailed, 30 HandshakeTimeout, 31 ConnectionFailed, 32 ApTsfReset,
33 UnmappedErrorCode, 34 UnknownConfigurationItem, 35 CannotStartAccessPoint,
36 DhcpConfigurationError, 37 AccessPointNotStarted, 38 AccessPointAlreadyStarted,
39 NotImplemented, 40 FileNotFound, 41 ThreadPoolIsFull,
42 UnexpectedCoprocessorRestart, 43-45 Invalid{,WiFi,Cell}ConfigurationFile,
46 NetworkDeadlock, 47 BatteryLevelCannotBeRead, 48 CannotStopBluetooth,
49 BluetoothAlreadyStarted, 50-60 EspReset* (see the `+++RST` table),
61 InvalidRequest, 62 InvalidCountryCode.

## Core dump traffic `(repo)`

The boot log reports the partition state before any of this starts:
`Found core dump 19044 bytes in flash @ 0x3b0000` together with
`Core dump data checksum is correct` means the image was present and valid
**at boot** `(capture)`.

The STM32 retrieves it over System (interface 4):

| Function | Request payload | Response payload |
|---|---|---|
| 15 GetCoreDumpInformation | none | 9 bytes: `IsValid` u8, `CoreDumpSize` u32, `PartitionSize` u32 |
| 16 GetCoreDumpFragment | 8 bytes: `Offset` u32, `Size` u32 | `Size` u32, then `Size` bytes of data |
| 17 EraseCoreDump | none | none. The status says whether it worked |

Refusals come back as the status code on the response:

- `CrcError` (1): the image checksum failed. **This is checked only on the
  fragment at offset 0**, which reads the whole partition and takes 900-1000
  ms. That delay is expected.
- `InvalidRequest` (61): no payload, `Size` 0, or an offset at or past the end.
  **The designed contract is that a request running past the end gets a short
  reply containing the rest of the dump.** The STM32 always asks for 4000 and
  advances by the size in the reply. Commit `450758f9` (2026-09-19) changed the
  ESP32 to refuse such requests, and that caused a boot loop (see *Capturing a
  boot loop*). Issue #501 (PR #502) restores the short reply. If
  `InvalidRequest` comes back for the last fragment, the ESP32 firmware predates
  that fix.
- `EspOutOfMemory` (24): the ESP32 could not allocate the fragment buffer.
- `Failure` (3): no coredump partition, or the size word read back as 0 or
  larger than the partition.

On the ESP32 side, the `CoreDump` tag prints `GetInformation: Enter/Exit`,
`GetFragment: Enter/Exit` and `Erase: Enter/Exit` when `System` or `CoreDump`
is in `LogComponents`.

**Fragment response payload length is `4 + Size`** from #501 onwards. Earlier
firmware declared `8 + Size` but wrote only `4 + Size`, leaving 4 bytes of
uninitialised heap at the end. The STM32 decoder reads only the size and the
data, so that was harmless. The decoder flags a payload that is not `4 + Size`
as pre-#501 firmware.

**The STM32 ignores the response status** `(repo)`. `espcp_queue_message()`
returns `completed_ok` once the reply arrives, whatever status the ESP32 sent.
`espcp_core_dump.c` then parses the payload unchecked, for the information,
fragment and erase requests alike. **Any refusal therefore becomes a silent
STM32 reset**, not a logged error. This is raised as
WildernessLabs/Meadow.OS#647. Until it is fixed, a reset shortly after a
refused core dump request points at the refusal, not at something new.

### A healthy retrieval `(capture, 2026-09-24, #501 flashed)`

A 19044-byte dump, run through `tools/meadow_esp32_log.py`:

```
GetCoreDumpInformation  -> IsValid=1 CoreDumpSize=19044 PartitionSize=65536
GetCoreDumpFragment Offset=0     Size=4000 -> len=4004 Size=4000
GetCoreDumpFragment Offset=4000  Size=4000 -> len=4004 Size=4000
GetCoreDumpFragment Offset=8000  Size=4000 -> len=4004 Size=4000
GetCoreDumpFragment Offset=12000 Size=4000 -> len=4004 Size=4000
GetCoreDumpFragment Offset=16000 Size=4000 -> len=3048 Size=3044   <- short last fragment
EraseCoreDump                               -> CompletedOk
```

The STM32 erases only after saving the dump to its own file system, so
`EraseCoreDump` in the trace means the file was written. The next boot shows
no `Found core dump` line. With full tracing each fragment takes about 1.9 s,
so the sequence ends about 10 s after the ESP32 boots.

## Raw SPI frames (`SPI` tag) `(repo)`

With `SPI` in `LogComponents`, every received frame is logged as
`InboundMessageReceived: Received N bytes.` followed by a hex dump of the whole
frame. Outbound frames log `Encoding N bytes as a M byte packet`. The state
machine logs `ProcessMessages: Waiting for notification, current state: ...`
and a `**** New State ****` banner on each pass. That is a great deal of
traffic on the control link (see above).

Wire header: 27 bytes, little-endian (`Encoders::EncodeMessage`):

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | protocol number, always `0x01` |
| 1 | 4 | CRC32 |
| 5 | 2 | PacketOffset |
| 7 | 2 | PacketLength |
| 9 | 1 | MessageType |
| 10 | 1 | Interface |
| 11 | 4 | Function |
| 15 | 4 | StatusCode |
| 19 | 4 | MessageID |
| 23 | 4 | PayloadLength |
| 27 | PacketLength | payload bytes `[PacketOffset, +PacketLength)` |

The frame is then padded: it is rounded up to a multiple of 4 (minimum 8), and
4 bytes of `SPI_MESSAGE_OVERHEAD` are added, because the SPI peripheral can
corrupt the last few bytes. The CRC is calculated over the frame **less its last
4 bytes**, with the CRC field set to zero. A bad CRC logs
`InboundMessageReceived: CRC Error.`. The frame limit is 4092 bytes.

## Capturing a boot loop `(capture, 2026-09-24)`

Hold **both** consoles open at once, the ESP-Prog console and the ST-Link VCP,
and stamp every line with host time as it arrives. A boot loop needs no reset
trigger. Of the two, only the direct ESP32 port is complete:

- **The `ESP Log:` relay drops and garbles characters under trace load.** Lines
  arrive as `[    6.3erBase: ...`, and the relay runs seconds behind. It also
  dies with the STM32, so the ESP32's last lines before a board reset appear
  only on the direct port. Decode the direct port and use the STM32 log for the
  STM32's own lines.
- **Read `rst:` on the ESP32 before blaming it.** `POWERON_RESET` together with
  `+++RST,1`, with no panic text, means the reset line was pulled. The STM32
  reset the board, and with it the ESP32 (`meadow.md`). A real ESP32 fault
  shows `+++RST,4` after a Guru Meditation.
- Tracing a core dump retrieval puts **every fragment on the console as hex**:
  4000 bytes become about 250 log lines, which takes about 2 s at 115200. That
  is on the same wire as the control link, and it stretches each retrieval
  step to around 1.9 s.

`tools/meadow_capture.py` does the capture: both ports, one thread each, host
timestamps, raw bytes kept alongside. `tools/meadow_esp32_log.py` accepts that
output, and relayed `ESP Log:` lines, as they are.

## Verifying an ESP32 fix on the board `(measured, 2026-09-24)`

1. **Flash the app over JTAG**: `openocd -f flash-app.cfg` from the repo root,
   with the Espressif OpenOCD first on `PATH`. It writes `build/MeadowComms.bin`
   at 0x10000 and verifies it. `flash.sh --all` (`flash-all.cfg`) also writes
   the bootloader (0x1000) and partition table (0x8000). **Neither touches the
   coredump, NVS or littlefs partitions**, so a dump that reproduces a fault
   survives the flash. The STM32 resetting the ESP32 every ~10 s did not
   disturb an 11 s programming run.
2. **Do not read the silence that follows as success.** The flash ends with an
   ESP32-only `reset run`. The STM32 is not restarted, does not repeat its
   boot-time requests, and in this capture stopped resetting the board. Both
   consoles stayed at **zero bytes for 60 s**. That is what "the ESP32 rebooted
   behind the STM32's back" looks like, not a working fix.
3. **Reset the whole board through the STM32** with the capture already
   running. The STM32 resets the ESP32 during its own boot, so this exercises
   the real startup path:

   ```bash
   python3 tools/meadow_capture.py out 75 --stm /dev/cu.usbmodem<VCP> & sleep 2
   "$MEADOW_OPENOCD_DIR/src/openocd" -s "$MEADOW_OPENOCD_DIR/tcl" \
     -f interface/stlink.cfg -f target/stm32f7x.cfg \
     -c "reset_config none separate" -c "init; reset run; shutdown"
   wait
   ```

   Run this from the skill directory, or give the script's full path. The two
   OpenOCD builds can run one after the other because they use different
   probes, but never point them at the same one.
4. **Count the boots.** `grep -c 'initialization began' out.stm.log` should be
   1. Then decode `out.esp.log` and compare it with the healthy sequence above.
5. **Match the build flags** of the firmware being replaced, so that the before
   and after captures can be compared line for line.

## Wrong turns

1. **The STM32 was blamed for a contract change made on the ESP32.** The first
   reading of the boot loop called the STM32's fixed 4000-byte request a bug,
   because the ESP32 source said the caller "must ask for exactly what is
   left". That sentence had been written five days earlier, by the change that
   caused the fault. `git log -S'fragmentIsInRange'` found it in one command, and
   the older copy of the ESP32 code in Rebase still had the shortening. **Before
   calling one side of the link wrong, run `git log -S` on the check that
   rejects the request**, and compare with the other side's copy of the
   protocol.

## What has not been seen yet

- A raw `SPI` hex dump from a real capture. The layout above comes from the
  encoder source.
- `+++BSY`, and `+++RST` with any value other than 1.
- What actually resets the STM32 after a refused core dump request. The
  unchecked parse (Meadow.OS#647) is the likely cause, but it has not been
  caught under a debugger. The reset code `0x14` has not been decoded:
  nothing in the Rebase C code writes the top byte of BK30R. It may be RCC_CSR
  shifted right by 24, which would read as PINRST|SFTRST.
