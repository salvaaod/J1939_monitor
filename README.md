# J1939 MASTERvOLT BMS Monitor

A Windows desktop monitor for MASTERvOLT battery-management-system messages on a J1939 CAN bus. The application uses Tkinter for the UI and the GCAN / USBCAN Windows driver DLL (`ECanVci.dll`) through Python `ctypes`.

## Features

- **GCAN / USBCAN adapter connection** with editable device type, device index, CAN channel index, and monitor source address. The CAN channel is always initialized for 500 kbps.
- **Application-local DLL loading**: `ECanVci.dll` is always loaded internally from the same directory as the running script or bundled executable, with no operator DLL path field.
- **J1939 29-bit extended-frame support** for receiving and transmitting classic 8-byte CAN frames.
- **MASTERvOLT proprietary BMS PGN monitoring** for:
  - `0x00FF00`
  - `0x00FF01`
- **Live raw frame display** showing PGN, CAN ID, payload bytes, and last-update time.
- **Live decoded signal table** for pack voltage, pack current, pack temperature, remaining time, state of charge, and alarms, with per-signal timeout display after 10 seconds without fresh data.
- **J1939 network-management participation** including address claim handling and responses to requests for address claim and component identification.
- **Persistent operator settings** using the current-user Windows registry on Windows and a JSON file in the user home directory on non-Windows development systems.

## Requirements and Dependencies

### Hardware and drivers

- GCAN / USBCAN adapter, typically `USBCAN-II` (`device_type = 4`).
- GCAN Windows driver installed.
- A properly wired CAN/J1939 bus:
  - CAN-H to CAN-H
  - CAN-L to CAN-L
  - common ground if required by the installation
  - correct bus termination, typically 120 ohms at both ends of the trunk
- The MASTERvOLT BMS or simulator transmitting the proprietary PGNs monitored by this application.

### Software

- Windows for real hardware operation, because the GCAN driver DLL is loaded with `ctypes.WinDLL`.
- Python 3.10 or newer is recommended because the source uses modern type-hint syntax such as `int | None`.
- Python standard-library modules only:
  - `ctypes`
  - `json`
  - `queue`
  - `sys`
  - `threading`
  - `time`
  - `tkinter`
  - `dataclasses`
  - `pathlib`
  - `typing`
  - `winreg` on Windows
- `ECanVci.dll` matching your Python process architecture:
  - 64-bit Python requires a 64-bit DLL.
  - 32-bit Python requires a 32-bit DLL.

No third-party Python packages are required.

## DLL Placement

Place `ECanVci.dll` in the same directory as the application:

```text
J1939_monitor/
├── ECanVci.dll
├── j1939_bms_monitor.py
└── README.md
```

The DLL path is intentionally not shown or edited in the UI. The application always uses the `ECanVci.dll` file located beside `j1939_bms_monitor.py` or beside the packaged executable.

## Running the Application

From the application directory on Windows:

```powershell
python .\j1939_bms_monitor.py
```

To print the startup window size and table column widths to the console while launching the app, add `--export`:

```powershell
python .\j1939_bms_monitor.py --export
```

The export is printed as JSON with `window_geometry`, `window_size`, `pgn_column_widths`, and `signal_column_widths` fields.

Adjust **Monitor SA** if needed, then click **Start monitoring**.

## Default Connection Settings

The adapter/channel settings use fixed application defaults and are no longer editable in the UI. **Monitor SA** remains editable beside the **Start monitoring** button.

| Setting | Default | Meaning |
| --- | ---: | --- |
| Device type | `4` | GCAN `USBCAN-II` |
| Device index | `0` | First connected adapter |
| CAN index | `0` | First CAN channel |
| Monitor SA | `0x80` | Preferred J1939 source address for this monitor |

The supplied MASTERvOLT matrix expects 500 kbps, so the monitor always uses the GCAN 500 kbps timing bytes (`Timing0 = 0x00`, `Timing1 = 0x1C`).

## Code Overview

The application is contained in `j1939_bms_monitor.py` and is organized into the following sections.

### Constants and DLL path helpers

The top of the file defines GCAN device defaults, fixed 500 kbps timing bytes, and J1939 PGNs. The helper functions `app_directory()` and `default_dll_path()` keep DLL loading internal and always point to `ECanVci.dll` in the same directory as the running script or packaged executable.

### GCAN structures

`CAN_OBJ` and `INIT_CONFIG` are `ctypes.Structure` definitions matching the GCAN DLL API data structures. They describe CAN frame payloads and CAN-channel initialization settings.

### Configuration and signal definitions

`DeviceConfig` stores adapter and channel settings while keeping the CAN timing fixed at 500 kbps. `SignalDefinition` describes each decoded BMS signal: PGN, label, byte/bit position, bit length, scaling factor, offset, engineering unit, and optional not-available or enumerated values.

The `SIGNALS` tuple contains the decode matrix for the two monitored proprietary PGNs.

### Persistent settings

`SettingsStore` loads and saves UI settings. On Windows it stores JSON in:

```text
HKEY_CURRENT_USER\Software\J1939BmsMonitor
```

On non-Windows systems it uses:

```text
~/.j1939_bms_monitor_settings.json
```

This keeps development possible outside Windows while preserving operator settings on production systems.

### J1939 helpers

The helper functions build and parse 29-bit J1939 identifiers, convert PGNs to request payload bytes, create an arbitrary-address-capable J1939 NAME, format payload bytes as hex, and extract little-endian signal fields.

### GCANDevice

`GCANDevice` wraps the DLL calls:

1. `OpenDevice`
2. `InitCAN`
3. `StartCAN`
4. `Transmit`
5. `Receive`
6. `CloseDevice`

Transmit and receive paths set and expect 29-bit extended CAN frames, which J1939 requires.

### J1939Node

`J1939Node` implements the monitor's basic network-management behavior:

- Sends an address-claim frame when monitoring starts.
- Handles incoming address claims and chooses a new address if another node with a lower NAME wins the preferred address.
- Responds to PGN requests for address claim.
- Responds to component-identification requests using J1939 transport-protocol BAM frames.

### MonitorWorker

`MonitorWorker` runs hardware communication in a background thread. It opens the GCAN adapter, starts the J1939 node, receives frames, handles network-management traffic, filters the monitored PGNs, and posts UI-safe events through a `queue.Queue`.

### BmsMonitorApp

`BmsMonitorApp` builds the Tkinter UI, validates the user-entered monitor source address, starts and stops the worker thread, polls worker events, updates the raw PGN table, decodes signal values, and saves operator settings when the window closes.

## Monitored Signals

| PGN | Signal | Scaling / Values | Unit |
| --- | --- | --- | --- |
| `0x00FF00` | Battery pack voltage | raw × 0.05 | V |
| `0x00FF00` | Battery pack net current | raw × 0.05 − 1000 | A |
| `0x00FF00` | Battery pack temperature | raw − 40 | deg C |
| `0x00FF01` | Remaining Time | raw | minutes |
| `0x00FF01` | Battery pack SOC | raw × 0.0025 | % |
| `0x00FF01` | LowLevel Alarm | `0` = no, `1` = YES | |
| `0x00FF01` | CriticalLow Alarm | `0` = no, `1` = YES | |
| `0x00FF01` | Reserved Alarm 3-8 | `0` = no, `1` = YES | |

## Troubleshooting

- If the app reports that the DLL cannot be loaded, confirm `ECanVci.dll` is in the same directory as `j1939_bms_monitor.py` or the packaged executable.
- If the DLL loads but the device will not open, confirm the GCAN driver is installed and the adapter is visible to Windows.
- If no frames update, confirm the bus is running at 500 kbps, check CAN wiring and termination, and confirm that the BMS is transmitting `0x00FF00` and `0x00FF01`.
- If Python reports an architecture error while loading the DLL, use a DLL build that matches your Python architecture.
