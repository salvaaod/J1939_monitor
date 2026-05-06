"""Tkinter J1939 BMS monitor for a GCAN / USBCAN adapter.

The app monitors the two proprietary MASTERvOLT J1939 BMS PGNs shown in the
provided CAN matrix and participates in the core J1939 network-management
protocols needed for address claiming and product identification.

Run on Windows with the GCAN driver installed and ECanVci.dll placed in the
same directory as this script or bundled executable::

    python j1939_bms_monitor.py
"""

from __future__ import annotations

import argparse
import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Iterable

if sys.platform == "win32":
    import winreg


# ---------------------------------------------------------------------------
# GCAN / USBCAN constants
# ---------------------------------------------------------------------------

USBCAN_I = 3
USBCAN_II = 4
DEFAULT_DEVICE_TYPE = USBCAN_II
DEFAULT_DEVICE_INDEX = 0
DEFAULT_CAN_INDEX = 0
DEFAULT_DLL_NAME = "ECanVci.dll"
DEFAULT_WINDOW_GEOMETRY = "480x570+99+79"
SETTINGS_REGISTRY_PATH = r"Software\J1939BmsMonitor"
SETTINGS_REGISTRY_VALUE = "Settings"
SETTINGS_FILE_NAME = ".j1939_bms_monitor_settings.json"

TIMING0_250K = 0x01
TIMING1_250K = 0x1C
TIMING0_500K = 0x00
TIMING1_500K = 0x1C


def app_directory() -> Path:
    """Return the directory that contains the running script or executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_dll_path() -> str:
    """Default to the GCAN DLL shipped beside the running application."""
    return str(app_directory() / DEFAULT_DLL_NAME)


# ---------------------------------------------------------------------------
# J1939 constants
# ---------------------------------------------------------------------------

GLOBAL_ADDRESS = 0xFF
NULL_ADDRESS = 0xFE
PREFERRED_SOURCE_ADDRESS = 0x80
PRIORITY_NETWORK_MANAGEMENT = 6
PRIORITY_INFO = 6

PGN_REQUEST = 0x00EA00
PGN_ADDRESS_CLAIMED = 0x00EE00
PGN_TP_CM = 0x00EC00
PGN_TP_DT = 0x00EB00
PGN_COMPONENT_IDENTIFICATION = 0x00FEEB

# The two proprietary PGNs from the supplied matrix.  The example IDs are
# 18FF00F3 and 18FF01F3, where F3 is the transmitting source address.
PGN_PROP_00 = 0x00FF00
PGN_PROP_01 = 0x00FF01
MONITORED_PGNS = (PGN_PROP_00, PGN_PROP_01)
SIGNAL_TIMEOUT_SECONDS = 10.0
NO_FRAME_TEXT = "No frame"
TIMEOUT_TEXT = "timeout"


# ---------------------------------------------------------------------------
# GCAN DLL structures
# ---------------------------------------------------------------------------


class CAN_OBJ(ctypes.Structure):
    _fields_ = [
        ("ID", ctypes.c_uint),
        ("TimeStamp", ctypes.c_uint),
        ("TimeFlag", ctypes.c_ubyte),
        ("SendType", ctypes.c_ubyte),
        ("RemoteFlag", ctypes.c_ubyte),
        ("ExternFlag", ctypes.c_ubyte),
        ("DataLen", ctypes.c_ubyte),
        ("Data", ctypes.c_ubyte * 8),
        ("Reserved", ctypes.c_ubyte * 3),
    ]


class INIT_CONFIG(ctypes.Structure):
    _fields_ = [
        ("AccCode", ctypes.c_uint),
        ("AccMask", ctypes.c_uint),
        ("Reserved", ctypes.c_uint),
        ("Filter", ctypes.c_ubyte),
        ("Timing0", ctypes.c_ubyte),
        ("Timing1", ctypes.c_ubyte),
        ("Mode", ctypes.c_ubyte),
    ]


@dataclass(frozen=True)
class DeviceConfig:
    device_type: int = DEFAULT_DEVICE_TYPE
    device_index: int = DEFAULT_DEVICE_INDEX
    can_index: int = DEFAULT_CAN_INDEX
    # The supplied MASTERvOLT matrix specifies 500 kbps.
    timing0: int = TIMING0_500K
    timing1: int = TIMING1_500K


@dataclass(frozen=True)
class ParsedId:
    priority: int
    pgn: int
    source_address: int
    destination_address: int | None


@dataclass(frozen=True)
class SignalDefinition:
    pgn: int
    label: str
    start_byte: int
    start_bit: int
    bit_length: int
    factor: float
    offset: float
    unit: str = ""
    na_value: int | None = None
    value_map: dict[int, str] | None = None

    @property
    def start_bit_index(self) -> int:
        return self.start_byte * 8 + self.start_bit


ALARM_VALUE_MAP = {0: "no", 1: "YES"}

SIGNALS: tuple[SignalDefinition, ...] = (
    SignalDefinition(PGN_PROP_00, "Battery pack voltage", 0, 0, 16, 0.05, 0.0, "V", 0xFFFF),
    SignalDefinition(PGN_PROP_00, "Battery pack net current", 2, 0, 16, 0.05, -1000.0, "A", 0xFFFF),
    SignalDefinition(PGN_PROP_00, "Battery pack temperature", 4, 0, 8, 1.0, -40.0, "deg C", 0xFF),
    SignalDefinition(PGN_PROP_01, "Remaining Time", 1, 0, 16, 1.0, 0.0, "minutes", 0xFFFF),
    SignalDefinition(PGN_PROP_01, "Battery pack SOC", 3, 0, 16, 0.0025, 0.0, "%", 0xFFFF),
    SignalDefinition(PGN_PROP_01, "LowLevel Alarm", 0, 0, 1, 1.0, 0.0, "", None, ALARM_VALUE_MAP),
    SignalDefinition(PGN_PROP_01, "CriticalLow Alarm", 0, 1, 1, 1.0, 0.0, "", None, ALARM_VALUE_MAP),
    SignalDefinition(PGN_PROP_01, "Reserved Alarm 3", 0, 2, 1, 1.0, 0.0, "", None, ALARM_VALUE_MAP),
    SignalDefinition(PGN_PROP_01, "Reserved Alarm 4", 0, 3, 1, 1.0, 0.0, "", None, ALARM_VALUE_MAP),
    SignalDefinition(PGN_PROP_01, "Reserved Alarm 5", 0, 4, 1, 1.0, 0.0, "", None, ALARM_VALUE_MAP),
    SignalDefinition(PGN_PROP_01, "Reserved Alarm 6", 0, 5, 1, 1.0, 0.0, "", None, ALARM_VALUE_MAP),
    SignalDefinition(PGN_PROP_01, "Reserved Alarm 7", 0, 6, 1, 1.0, 0.0, "", None, ALARM_VALUE_MAP),
    SignalDefinition(PGN_PROP_01, "Reserved Alarm 8", 0, 7, 1, 1.0, 0.0, "", None, ALARM_VALUE_MAP),
)



# ---------------------------------------------------------------------------
# Persistent application settings
# ---------------------------------------------------------------------------


DEFAULT_PGN_COLUMN_WIDTHS: dict[str, int] = {
    "pgn": 83,
    "can_id": 102,
    "payload": 162,
    "age": 91,
}
DEFAULT_SIGNAL_COLUMN_WIDTHS: dict[str, int] = {
    "pgn": 76,
    "signal": 174,
    "raw": 68,
    "value": 66,
    "unit": 54,
}


class SettingsStore:
    """Persist operator-adjustable UI and connection settings.

    Windows builds store the JSON payload in the current user's registry.  A
    small JSON file is used on other platforms so the app remains runnable for
    development and tests outside Windows.
    """

    def load(self) -> dict[str, Any]:
        if sys.platform == "win32":
            return self._load_from_registry()
        return self._load_from_file()

    def save(self, settings: dict[str, Any]) -> None:
        if sys.platform == "win32":
            self._save_to_registry(settings)
        else:
            self._save_to_file(settings)

    def _load_from_registry(self) -> dict[str, Any]:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_REGISTRY_PATH) as key:
                payload, _value_type = winreg.QueryValueEx(key, SETTINGS_REGISTRY_VALUE)
        except OSError:
            return {}
        return self._parse_payload(payload)

    def _save_to_registry(self, settings: dict[str, Any]) -> None:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_REGISTRY_PATH) as key:
            winreg.SetValueEx(key, SETTINGS_REGISTRY_VALUE, 0, winreg.REG_SZ, json.dumps(settings, sort_keys=True))

    def _load_from_file(self) -> dict[str, Any]:
        settings_path = Path.home() / SETTINGS_FILE_NAME
        try:
            payload = settings_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        return self._parse_payload(payload)

    def _save_to_file(self, settings: dict[str, Any]) -> None:
        settings_path = Path.home() / SETTINGS_FILE_NAME
        settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _parse_payload(payload: object) -> dict[str, Any]:
        if not isinstance(payload, str):
            return {}
        try:
            settings = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        if isinstance(settings, dict):
            return settings
        return {}


def merged_column_widths(saved_widths: object, default_widths: dict[str, int]) -> dict[str, int]:
    widths = dict(default_widths)
    if not isinstance(saved_widths, dict):
        return widths
    for column in default_widths:
        try:
            width = int(saved_widths[column])
        except (KeyError, TypeError, ValueError):
            continue
        if width > 0:
            widths[column] = width
    return widths


def setting_as_str(settings: dict[str, Any], key: str, default: str) -> str:
    value = settings.get(key, default)
    if isinstance(value, str) and value:
        return value
    return default

# ---------------------------------------------------------------------------
# J1939 helpers
# ---------------------------------------------------------------------------


def j1939_id(priority: int, pgn: int, source_address: int, destination_address: int | None = None) -> int:
    """Build a 29-bit J1939 identifier for PDU1 or PDU2 PGNs."""
    pf = (pgn >> 8) & 0xFF
    if pf < 240:
        ps = GLOBAL_ADDRESS if destination_address is None else destination_address & 0xFF
        pgn_field = (pgn & 0x3FF00) | ps
    else:
        pgn_field = pgn & 0x3FFFF
    return ((priority & 0x7) << 26) | (pgn_field << 8) | (source_address & 0xFF)


def parse_j1939_id(can_id: int) -> ParsedId:
    priority = (can_id >> 26) & 0x7
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    source_address = can_id & 0xFF
    if pf < 240:
        pgn = pf << 8
        destination_address: int | None = ps
    else:
        pgn = (pf << 8) | ps
        destination_address = None
    return ParsedId(priority, pgn, source_address, destination_address)


def pgn_to_bytes(pgn: int) -> list[int]:
    return [pgn & 0xFF, (pgn >> 8) & 0xFF, (pgn >> 16) & 0xFF]


def pgn_from_request_payload(data: bytes) -> int | None:
    if len(data) < 3:
        return None
    return int(data[0]) | (int(data[1]) << 8) | (int(data[2]) << 16)


def make_name(identity_number: int = 0x1939A, manufacturer_code: int = 0x7FF) -> int:
    """Create a valid arbitrary-address-capable J1939 NAME value.

    The manufacturer code defaults to 0x7FF as a placeholder and should be
    replaced with the real assigned manufacturer code before product release.
    """
    name = 0
    name |= identity_number & 0x1FFFFF
    name |= (manufacturer_code & 0x7FF) << 21
    name |= (0 & 0x7) << 32  # ECU instance
    name |= (0 & 0x1F) << 35  # function instance
    name |= (130 & 0xFF) << 40  # function: instrumentation/general monitor placeholder
    name |= (0 & 0x1) << 48  # reserved
    name |= (0 & 0x7F) << 49  # vehicle system
    name |= (0 & 0xF) << 56  # vehicle system instance
    name |= (0 & 0x7) << 60  # industry group: global
    name |= 1 << 63  # arbitrary address capable
    return name


def bytes_hex(data: Iterable[int]) -> str:
    return " ".join(f"{int(byte) & 0xFF:02X}" for byte in data)


def extract_little_endian(data: bytes, start_bit: int, length: int) -> int:
    raw = int.from_bytes(data.ljust(8, b"\x00")[:8], "little")
    mask = (1 << length) - 1
    return (raw >> start_bit) & mask


def format_raw_value(raw: int, bit_length: int) -> str:
    """Format raw signal data with enough leading zeros for its bit length."""
    hex_digits = max(1, (bit_length + 3) // 4)
    return f"0x{raw:0{hex_digits}X}"


def format_scaled_value(value: float, definition: SignalDefinition) -> str:
    if definition.factor < 0.01:
        text = f"{abs(value):.3f}"
    elif definition.factor < 1:
        text = f"{abs(value):.2f}"
    else:
        text = f"{abs(value):.0f}"
    sign = "-" if value < 0 else " "
    return f"{sign}{text}"


def format_signal_value(definition: SignalDefinition, data: bytes) -> tuple[str, str]:
    raw = extract_little_endian(data, definition.start_bit_index, definition.bit_length)
    raw_text = format_raw_value(raw, definition.bit_length)
    if definition.na_value is not None and raw == definition.na_value:
        return "N/A", raw_text
    if definition.value_map:
        return definition.value_map.get(raw, str(raw)), raw_text
    scaled = raw * definition.factor + definition.offset
    return format_scaled_value(scaled, definition), raw_text


# ---------------------------------------------------------------------------
# GCAN device wrapper
# ---------------------------------------------------------------------------


class GCANDevice:
    def __init__(self, config: DeviceConfig):
        self.config = config
        self.dll = ctypes.WinDLL(default_dll_path())
        self._bind_functions()

    def _bind_functions(self) -> None:
        self.dll.OpenDevice.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        self.dll.OpenDevice.restype = ctypes.c_uint
        self.dll.CloseDevice.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.dll.CloseDevice.restype = ctypes.c_uint
        self.dll.InitCAN.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(INIT_CONFIG)]
        self.dll.InitCAN.restype = ctypes.c_uint
        self.dll.StartCAN.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        self.dll.StartCAN.restype = ctypes.c_uint
        self.dll.Transmit.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(CAN_OBJ), ctypes.c_ulong]
        self.dll.Transmit.restype = ctypes.c_ulong
        self.dll.Receive.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(CAN_OBJ), ctypes.c_ulong, ctypes.c_int]
        self.dll.Receive.restype = ctypes.c_ulong

    def open(self) -> None:
        if self.dll.OpenDevice(self.config.device_type, self.config.device_index, 0) == 0:
            raise RuntimeError("OpenDevice failed")
        init_config = INIT_CONFIG(
            AccCode=0,
            AccMask=0xFFFFFFFF,
            Reserved=0,
            Filter=0,
            Timing0=self.config.timing0,
            Timing1=self.config.timing1,
            Mode=0,
        )
        if self.dll.InitCAN(self.config.device_type, self.config.device_index, self.config.can_index, ctypes.byref(init_config)) == 0:
            self.close()
            raise RuntimeError("InitCAN failed")
        if self.dll.StartCAN(self.config.device_type, self.config.device_index, self.config.can_index) == 0:
            self.close()
            raise RuntimeError("StartCAN failed")

    def close(self) -> None:
        self.dll.CloseDevice(self.config.device_type, self.config.device_index)

    def send(self, frame_id: int, data: bytes | list[int]) -> int:
        payload = bytes(data)
        if len(payload) > 8:
            raise ValueError("Classic CAN payload must be 8 bytes or less")
        can_obj = CAN_OBJ()
        can_obj.ID = frame_id
        can_obj.TimeStamp = 0
        can_obj.TimeFlag = 0
        can_obj.SendType = 0
        can_obj.RemoteFlag = 0
        can_obj.ExternFlag = 1
        can_obj.DataLen = len(payload)
        for index, value in enumerate(payload):
            can_obj.Data[index] = value
        return int(
            self.dll.Transmit(
                self.config.device_type,
                self.config.device_index,
                self.config.can_index,
                ctypes.byref(can_obj),
                1,
            )
        )

    def receive(self, max_frames: int = 100, wait_ms: int = 50) -> list[tuple[int, bytes]]:
        frame_array = (CAN_OBJ * max_frames)()
        count = int(
            self.dll.Receive(
                self.config.device_type,
                self.config.device_index,
                self.config.can_index,
                frame_array,
                max_frames,
                wait_ms,
            )
        )
        frames: list[tuple[int, bytes]] = []
        for index in range(min(count, max_frames)):
            frame = frame_array[index]
            if frame.ExternFlag and not frame.RemoteFlag:
                frames.append((int(frame.ID), bytes(frame.Data[: frame.DataLen])))
        return frames


# ---------------------------------------------------------------------------
# J1939 network-management node
# ---------------------------------------------------------------------------


class J1939Node:
    def __init__(self, device: GCANDevice, source_address: int = PREFERRED_SOURCE_ADDRESS):
        self.device = device
        self.source_address = source_address
        self.name = make_name()
        self.product_text = "Mastervolt BMS Monitor*OpenAI Codex*J1939 Tkinter Monitor*1.0*"
        self._next_address = source_address

    def send_address_claim(self) -> None:
        self.device.send(
            j1939_id(PRIORITY_NETWORK_MANAGEMENT, PGN_ADDRESS_CLAIMED, self.source_address, GLOBAL_ADDRESS),
            self.name.to_bytes(8, "little"),
        )

    def send_cannot_claim(self) -> None:
        self.device.send(
            j1939_id(PRIORITY_NETWORK_MANAGEMENT, PGN_ADDRESS_CLAIMED, NULL_ADDRESS, GLOBAL_ADDRESS),
            self.name.to_bytes(8, "little"),
        )

    def handle_frame(self, can_id: int, data: bytes) -> None:
        parsed = parse_j1939_id(can_id)
        if parsed.pgn == PGN_REQUEST:
            requested_pgn = pgn_from_request_payload(data)
            if self._is_for_this_node(parsed.destination_address):
                self._handle_request(requested_pgn, parsed.source_address)
        elif parsed.pgn == PGN_ADDRESS_CLAIMED and len(data) >= 8:
            self._handle_address_claim(parsed.source_address, int.from_bytes(data[:8], "little"))

    def _is_for_this_node(self, destination_address: int | None) -> bool:
        return destination_address in (GLOBAL_ADDRESS, self.source_address)

    def _handle_request(self, requested_pgn: int | None, requester: int) -> None:
        if requested_pgn == PGN_ADDRESS_CLAIMED:
            self.send_address_claim()
        elif requested_pgn == PGN_COMPONENT_IDENTIFICATION:
            self.send_component_identification(requester)

    def _handle_address_claim(self, claimed_address: int, other_name: int) -> None:
        if claimed_address != self.source_address or other_name == self.name:
            return
        if other_name < self.name:
            self._choose_new_address()
        else:
            self.send_address_claim()

    def _choose_new_address(self) -> None:
        for offset in range(1, 253):
            candidate = (self._next_address + offset) % 254
            if candidate not in (GLOBAL_ADDRESS, NULL_ADDRESS):
                self.source_address = candidate
                self._next_address = candidate
                self.send_address_claim()
                return
        self.source_address = NULL_ADDRESS
        self.send_cannot_claim()

    def send_component_identification(self, requester: int = GLOBAL_ADDRESS) -> None:
        payload = self.product_text.encode("ascii", errors="replace")
        self._send_bam(PGN_COMPONENT_IDENTIFICATION, payload)

    def _send_bam(self, pgn: int, payload: bytes) -> None:
        total_size = len(payload)
        packet_count = (total_size + 6) // 7
        cm_data = bytes([0x20, total_size & 0xFF, (total_size >> 8) & 0xFF, packet_count, 0xFF, *pgn_to_bytes(pgn)])
        self.device.send(j1939_id(PRIORITY_INFO, PGN_TP_CM, self.source_address, GLOBAL_ADDRESS), cm_data)
        time.sleep(0.05)
        for sequence in range(1, packet_count + 1):
            chunk = payload[(sequence - 1) * 7 : sequence * 7]
            dt_data = bytes([sequence]) + chunk.ljust(7, b"\xFF")
            self.device.send(j1939_id(PRIORITY_INFO, PGN_TP_DT, self.source_address, GLOBAL_ADDRESS), dt_data)
            time.sleep(0.02)


class MonitorWorker(threading.Thread):
    def __init__(
        self,
        config: DeviceConfig,
        source_address: int,
        event_queue: queue.Queue[tuple[str, object]],
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True)
        self.config = config
        self.source_address = source_address
        self.event_queue = event_queue
        self.stop_event = stop_event

    def run(self) -> None:
        device: GCANDevice | None = None
        try:
            device = GCANDevice(self.config)
            device.open()
            node = J1939Node(device, self.source_address)
            node.send_address_claim()
            self.event_queue.put(("status", f"Connected, claimed source address 0x{node.source_address:02X}"))
            while not self.stop_event.is_set():
                for can_id, data in device.receive():
                    node.handle_frame(can_id, data)
                    parsed = parse_j1939_id(can_id)
                    if parsed.pgn in MONITORED_PGNS:
                        self.event_queue.put(("frame", (can_id, data, parsed)))
        except Exception as exc:  # noqa: BLE001 - worker must report all hardware/DLL failures to the UI
            self.event_queue.put(("error", str(exc)))
        finally:
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
            self.event_queue.put(("stopped", None))


# ---------------------------------------------------------------------------
# Tkinter user interface
# ---------------------------------------------------------------------------


class BmsMonitorApp(tk.Tk):
    def __init__(self, export_layout: bool = False):
        super().__init__()
        self.settings_store = SettingsStore()
        self.settings = self.settings_store.load()
        self.title("J1939 MASTERVOLT BMS Monitor")
        self.geometry(setting_as_str(self.settings, "window_geometry", DEFAULT_WINDOW_GEOMETRY))
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: MonitorWorker | None = None
        self.signal_rows: dict[str, str] = {}
        self.signal_update_times: dict[str, float] = {}
        self.timed_out_signals: set[str] = set()
        self.pgn_rows: dict[int, str] = {}
        self.pgn_update_times: dict[int, float] = {}
        self.timed_out_pgns: set[int] = set()
        self._build_ui()
        if export_layout:
            self._print_layout_export()
        self.after(100, self._poll_worker)

    def _build_ui(self) -> None:
        connection = ttk.LabelFrame(self, text="GCAN / USBCAN connection")
        connection.pack(fill="x", padx=10, pady=8)

        self.source_address_var = tk.StringVar(
            value=setting_as_str(self.settings, "source_address", f"0x{PREFERRED_SOURCE_ADDRESS:02X}")
        )
        self.status_var = tk.StringVar(value="Disconnected")

        self.start_button = ttk.Button(connection, text="Start monitoring", command=self.start_monitoring)
        self.start_button.grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Label(connection, text="Monitor SA").grid(row=0, column=1, sticky="w", padx=(8, 2), pady=6)
        ttk.Entry(connection, textvariable=self.source_address_var, width=8).grid(
            row=0, column=2, sticky="w", padx=(0, 8), pady=6
        )
        ttk.Label(connection, textvariable=self.status_var).grid(row=0, column=3, sticky="w", padx=8, pady=6)
        connection.columnconfigure(3, weight=1)

        pgn_frame = ttk.LabelFrame(self, text="Current monitored PGN frames")
        pgn_frame.pack(fill="x", padx=10, pady=6)
        self.pgn_tree = ttk.Treeview(pgn_frame, columns=("pgn", "can_id", "payload", "age"), show="headings", height=3)
        pgn_column_widths = merged_column_widths(self.settings.get("pgn_column_widths"), DEFAULT_PGN_COLUMN_WIDTHS)
        for column, heading, width in (
            ("pgn", "PGN", pgn_column_widths["pgn"]),
            ("can_id", "CAN ID", pgn_column_widths["can_id"]),
            ("payload", "Payload (hex)", pgn_column_widths["payload"]),
            ("age", "Last update", pgn_column_widths["age"]),
        ):
            self.pgn_tree.heading(column, text=heading)
            self.pgn_tree.column(column, width=width, anchor="w")
        self.pgn_tree.pack(fill="x", padx=8, pady=8)
        for pgn in MONITORED_PGNS:
            item = self.pgn_tree.insert("", "end", values=(f"0x{pgn:05X}", "-", "-", "never"))
            self.pgn_rows[pgn] = item

        signals_frame = ttk.LabelFrame(self, text="Decoded signal values")
        signals_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.signal_tree = ttk.Treeview(signals_frame, columns=("pgn", "signal", "raw", "value", "unit"), show="headings")
        signal_column_widths = merged_column_widths(self.settings.get("signal_column_widths"), DEFAULT_SIGNAL_COLUMN_WIDTHS)
        for column, heading, width in (
            ("pgn", "PGN", signal_column_widths["pgn"]),
            ("signal", "Signal", signal_column_widths["signal"]),
            ("raw", "Raw", signal_column_widths["raw"]),
            ("value", "Value", signal_column_widths["value"]),
            ("unit", "Units", signal_column_widths["unit"]),
        ):
            self.signal_tree.heading(column, text=heading)
            self.signal_tree.column(column, width=width, anchor="w")
        self.signal_tree.pack(fill="both", expand=True, padx=8, pady=8)
        for definition in SIGNALS:
            key = self._signal_key(definition)
            item = self.signal_tree.insert(
                "",
                "end",
                values=(f"0x{definition.pgn:05X}", definition.label, "-", NO_FRAME_TEXT, definition.unit),
            )
            self.signal_rows[key] = item

    def start_monitoring(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            source_address = int(self.source_address_var.get(), 0)
            if not 0 <= source_address <= 253:
                raise ValueError("Monitor SA must be between 0x00 and 0xFD")
            config = DeviceConfig()
        except Exception as exc:  # noqa: BLE001 - validation message is shown to operator
            messagebox.showerror("Invalid configuration", str(exc))
            return
        self.stop_event.clear()
        self._reset_pgn_rows()
        self._reset_signal_rows()
        self.worker = MonitorWorker(config, source_address, self.event_queue, self.stop_event)
        self.worker.start()
        self.start_button.configure(state="disabled")
        self.status_var.set("Connecting...")

    def stop_monitoring(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping...")

    def _poll_worker(self) -> None:
        while True:
            try:
                event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if event == "status":
                self.status_var.set(str(payload))
            elif event == "error":
                self.status_var.set(f"Error: {payload}")
                messagebox.showerror("Monitoring error", str(payload))
            elif event == "frame":
                can_id, data, parsed = payload  # type: ignore[misc]
                self._update_frame(int(can_id), bytes(data), parsed)
            elif event == "stopped":
                self.start_button.configure(state="normal")
                if not self.stop_event.is_set() and not self.status_var.get().startswith("Error"):
                    self.status_var.set("Disconnected")
                elif self.stop_event.is_set():
                    self.status_var.set("Stopped")
        self._expire_stale_pgns()
        self._expire_stale_signals()
        self.after(100, self._poll_worker)

    def _reset_pgn_rows(self) -> None:
        self.pgn_update_times.clear()
        self.timed_out_pgns.clear()
        for pgn, row in self.pgn_rows.items():
            self.pgn_tree.item(row, values=(f"0x{pgn:05X}", "-", "-", "never"))

    def _reset_signal_rows(self) -> None:
        self.signal_update_times.clear()
        self.timed_out_signals.clear()
        for definition in SIGNALS:
            key = self._signal_key(definition)
            row = self.signal_rows.get(key)
            if row:
                self.signal_tree.item(
                    row,
                    values=(f"0x{definition.pgn:05X}", definition.label, "-", NO_FRAME_TEXT, definition.unit),
                )

    def _expire_stale_pgns(self) -> None:
        now = time.monotonic()
        for pgn, row in self.pgn_rows.items():
            updated_at = self.pgn_update_times.get(pgn)
            if updated_at is None or pgn in self.timed_out_pgns:
                continue
            if now - updated_at <= SIGNAL_TIMEOUT_SECONDS:
                continue
            self.pgn_tree.item(row, values=(f"0x{pgn:05X}", "-", TIMEOUT_TEXT, TIMEOUT_TEXT))
            self.timed_out_pgns.add(pgn)

    def _expire_stale_signals(self) -> None:
        now = time.monotonic()
        for definition in SIGNALS:
            key = self._signal_key(definition)
            updated_at = self.signal_update_times.get(key)
            if updated_at is None or key in self.timed_out_signals:
                continue
            if now - updated_at <= SIGNAL_TIMEOUT_SECONDS:
                continue
            row = self.signal_rows[key]
            self.signal_tree.item(
                row,
                values=(f"0x{definition.pgn:05X}", definition.label, "-", TIMEOUT_TEXT, definition.unit),
            )
            self.timed_out_signals.add(key)

    def _update_frame(self, can_id: int, data: bytes, parsed: ParsedId) -> None:
        item = self.pgn_rows.get(parsed.pgn)
        timestamp = time.strftime("%H:%M:%S")
        updated_at = time.monotonic()
        if item:
            self.pgn_tree.item(
                item,
                values=(f"0x{parsed.pgn:05X}", f"0x{can_id:08X}", bytes_hex(data), timestamp),
            )
            self.pgn_update_times[parsed.pgn] = updated_at
            self.timed_out_pgns.discard(parsed.pgn)
        for definition in SIGNALS:
            if definition.pgn != parsed.pgn:
                continue
            value, raw = format_signal_value(definition, data)
            key = self._signal_key(definition)
            row = self.signal_rows[key]
            self.signal_tree.item(row, values=(f"0x{definition.pgn:05X}", definition.label, raw, value, definition.unit))
            self.signal_update_times[key] = updated_at
            self.timed_out_signals.discard(key)

    @staticmethod
    def _signal_key(definition: SignalDefinition) -> str:
        return f"{definition.pgn:05X}:{definition.label}"

    def _layout_export(self) -> dict[str, object]:
        self.update_idletasks()
        return {
            "window_geometry": self.geometry(),
            "window_size": {"width": self.winfo_width(), "height": self.winfo_height()},
            "pgn_column_widths": self._tree_column_widths(self.pgn_tree, DEFAULT_PGN_COLUMN_WIDTHS),
            "signal_column_widths": self._tree_column_widths(self.signal_tree, DEFAULT_SIGNAL_COLUMN_WIDTHS),
        }

    def _print_layout_export(self) -> None:
        print(json.dumps(self._layout_export(), indent=2, sort_keys=True), flush=True)

    def _tree_column_widths(self, tree: ttk.Treeview, columns: Iterable[str]) -> dict[str, int]:
        return {column: int(tree.column(column, "width")) for column in columns}

    def _collect_settings(self) -> dict[str, Any]:
        self.update_idletasks()
        return {
            "source_address": self.source_address_var.get().strip() or f"0x{PREFERRED_SOURCE_ADDRESS:02X}",
            "window_geometry": self.geometry(),
            "pgn_column_widths": self._tree_column_widths(self.pgn_tree, DEFAULT_PGN_COLUMN_WIDTHS),
            "signal_column_widths": self._tree_column_widths(self.signal_tree, DEFAULT_SIGNAL_COLUMN_WIDTHS),
        }

    def _save_settings(self) -> None:
        try:
            self.settings_store.save(self._collect_settings())
        except OSError:
            # Closing the monitor should not be blocked by a registry or file
            # permission problem.  Settings will fall back to defaults next run.
            pass

    def destroy(self) -> None:
        self.stop_event.set()
        self._save_settings()
        super().destroy()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tkinter J1939 MASTERVOLT BMS monitor")
    parser.add_argument(
        "--export",
        action="store_true",
        help="print the startup window geometry and table column widths to stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    BmsMonitorApp(export_layout=args.export).mainloop()


if __name__ == "__main__":
    main()
