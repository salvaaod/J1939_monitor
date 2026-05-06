# GCAN / USBCAN Windows Interface Guide

This guide explains how to use the GCAN / USBCAN Windows interface based on the code in `J1939_simulator.py`.

The simulator communicates with the GCAN adapter by loading `ECanVci.dll` directly from Python using `ctypes`.

---

## 1. Requirements

Use this guide on a Windows machine with:

1. GCAN / USBCAN hardware connected by USB.
2. GCAN Windows driver installed.
3. `ECanVci.dll` available on the machine.
4. Python installed.
5. Matching Python and DLL architecture:
   - 32-bit Python with 32-bit `ECanVci.dll`
   - 64-bit Python with 64-bit `ECanVci.dll`
6. A CAN/J1939 bus connected correctly:
   - CAN-H to CAN-H
   - CAN-L to CAN-L
   - common ground if required
   - correct 120 ohm termination
7. A receiving device configured for:
   - J1939
   - 250 kbps
   - 29-bit extended CAN identifiers

---

## 2. Important values from this simulator

The repository uses these GCAN / USBCAN settings:

    USBCAN_II = 4
    DEFAULT_DEVICE_TYPE = USBCAN_II
    DEFAULT_DEVICE_INDEX = 0
    DEFAULT_CAN_INDEX = 0
    DEFAULT_DLL_NAME = "ECanVci.dll"

    TIMING0_250K = 0x01
    TIMING1_250K = 0x1C

Meaning:

| Setting | Value | Description |
|---|---:|---|
| Device type | `4` | USBCAN-II |
| Device index | `0` | First connected adapter |
| CAN index | `0` | First CAN channel |
| DLL name | `ECanVci.dll` | GCAN Windows DLL |
| Baud rate | 250 kbps | Typical J1939 baud rate |
| Timing0 | `0x01` | GCAN baud-rate timing byte |
| Timing1 | `0x1C` | GCAN baud-rate timing byte |

For USBCAN-I, the device type is commonly:

    USBCAN_I = 3

---

## 3. GCAN DLL call sequence

The basic order is:

    OpenDevice(...)
    InitCAN(...)
    StartCAN(...)
    Transmit(...)
    CloseDevice(...)

In plain language:

1. Load `ECanVci.dll`.
2. Open the USB-CAN adapter.
3. Initialize the selected CAN channel.
4. Start the CAN channel.
5. Send CAN frames.
6. Close the device when finished.

---

## 4. Important J1939 notes

J1939 uses **29-bit extended CAN identifiers**.

Therefore, every transmitted J1939 frame must set:

    can_obj.ExternFlag = 1

Normal data frames should use:

    can_obj.RemoteFlag = 0

Classic CAN payloads are up to 8 bytes:

    can_obj.DataLen = len(data)

A successful transmit usually returns `1`, meaning one CAN frame was accepted by the DLL for transmission.

---

## 5. Complete Python example

Save this file as:

    gcan_j1939_example.py

Place it in the same folder as `ECanVci.dll`, or change `DLL_PATH` to the full DLL path.

    import ctypes
    import time
    from dataclasses import dataclass


    # ---------------------------------------------------------------------------
    # GCAN / USBCAN configuration
    # ---------------------------------------------------------------------------

    USBCAN_II = 4

    DEVICE_TYPE = USBCAN_II
    DEVICE_INDEX = 0
    CAN_INDEX = 0

    # J1939 commonly uses 250 kbps.
    TIMING0_250K = 0x01
    TIMING1_250K = 0x1C

    DLL_PATH = "ECanVci.dll"


    # ---------------------------------------------------------------------------
    # J1939 configuration
    # ---------------------------------------------------------------------------

    PGN_CCVS = 0x00FEF1  # Cruise Control / Vehicle Speed
    PGN_ETC2 = 0x00F005  # Electronic Transmission Controller 2
    PGN_OEL = 0x00FDCC   # Operator External Light Controls

    PRIORITY_DEFAULT = 6
    PRIORITY_OEL = 3

    SOURCE_ADDRESS = 0x00


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


    @dataclass
    class DeviceConfig:
        dll_path: str = DLL_PATH
        device_type: int = DEVICE_TYPE
        device_index: int = DEVICE_INDEX
        can_index: int = CAN_INDEX
        timing0: int = TIMING0_250K
        timing1: int = TIMING1_250K


    # ---------------------------------------------------------------------------
    # J1939 frame helpers
    # ---------------------------------------------------------------------------

    def j1939_id(priority: int, pgn: int, source_address: int) -> int:
        """
        Build a 29-bit J1939 CAN identifier.

        priority:
            0 to 7. Lower number means higher priority.

        pgn:
            18-bit J1939 PGN.

        source_address:
            0 to 255.
        """
        return ((priority & 0x7) << 26) | ((pgn & 0x3FFFF) << 8) | (source_address & 0xFF)


    def build_ccvs_data(speed_kmh: float, brake_active: bool) -> list[int]:
        """
        Build an 8-byte CCVS payload.

        SPN 84 wheel-based vehicle speed:
            resolution = 1/256 km/h per bit

        SPN 597 brake switch:
            represented here using byte 4 bits 5-6 in J1939 numbering.
        """
        raw_speed = int(max(0.0, min(float(speed_kmh), 250.996)) * 256)

        data = [0x00] * 8
        data[1] = raw_speed & 0xFF
        data[2] = (raw_speed >> 8) & 0xFF

        brake_switch = 0b01 if brake_active else 0b00
        data[3] = brake_switch << 4

        return data


    def build_etc2_data(gear_choice: str) -> list[int]:
        """
        Build an ETC2 payload for transmission current gear.

        Values used by the simulator:
            Reverse = 124
            Neutral = 125
            Drive   = 126
            Park    = 251
        """
        gear_map = {
            "Reverse": 124,
            "Neutral": 125,
            "Drive": 126,
            "Park": 251,
        }

        data = [0x00] * 8
        data[3] = gear_map.get(gear_choice, 125)
        return data


    def build_oel_data(left_active: bool, right_active: bool) -> list[int]:
        """
        Build an OEL payload for turn signals.

        In this simulator:
            data[1] bit 0 = left signal
            data[1] bit 1 = right signal
        """
        data = [0x00] * 8

        if left_active:
            data[1] |= 0x01

        if right_active:
            data[1] |= 0x02

        return data


    # ---------------------------------------------------------------------------
    # GCAN device wrapper
    # ---------------------------------------------------------------------------

    class GCANDevice:
        def __init__(self, config: DeviceConfig):
            self.config = config
            self.dll = ctypes.WinDLL(config.dll_path)
            self._bind_functions()

        def _bind_functions(self):
            self.dll.OpenDevice.argtypes = [
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            self.dll.OpenDevice.restype = ctypes.c_uint

            self.dll.CloseDevice.argtypes = [
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            self.dll.CloseDevice.restype = ctypes.c_uint

            self.dll.InitCAN.argtypes = [
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.POINTER(INIT_CONFIG),
            ]
            self.dll.InitCAN.restype = ctypes.c_uint

            self.dll.StartCAN.argtypes = [
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            self.dll.StartCAN.restype = ctypes.c_uint

            self.dll.Transmit.argtypes = [
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.POINTER(CAN_OBJ),
                ctypes.c_ulong,
            ]
            self.dll.Transmit.restype = ctypes.c_ulong

        def open(self):
            result = self.dll.OpenDevice(
                self.config.device_type,
                self.config.device_index,
                0,
            )

            if result == 0:
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

            result = self.dll.InitCAN(
                self.config.device_type,
                self.config.device_index,
                self.config.can_index,
                ctypes.byref(init_config),
            )

            if result == 0:
                self.close()
                raise RuntimeError("InitCAN failed")

            result = self.dll.StartCAN(
                self.config.device_type,
                self.config.device_index,
                self.config.can_index,
            )

            if result == 0:
                self.close()
                raise RuntimeError("StartCAN failed")

        def close(self):
            self.dll.CloseDevice(
                self.config.device_type,
                self.config.device_index,
            )

        def send(self, frame_id: int, data: list[int]) -> int:
            if len(data) > 8:
                raise ValueError("Classic CAN payload must be 8 bytes or less")

            can_obj = CAN_OBJ()

            can_obj.ID = frame_id
            can_obj.TimeStamp = 0
            can_obj.TimeFlag = 0

            # 0 = normal send
            can_obj.SendType = 0

            # 0 = data frame, not remote frame
            can_obj.RemoteFlag = 0

            # Important: J1939 uses 29-bit extended CAN IDs.
            can_obj.ExternFlag = 1

            can_obj.DataLen = len(data)

            for index, value in enumerate(data):
                can_obj.Data[index] = value & 0xFF

            sent_count = self.dll.Transmit(
                self.config.device_type,
                self.config.device_index,
                self.config.can_index,
                ctypes.byref(can_obj),
                1,
            )

            return int(sent_count)


    # ---------------------------------------------------------------------------
    # Example application
    # ---------------------------------------------------------------------------

    def main():
        config = DeviceConfig(
            dll_path=DLL_PATH,
            device_type=USBCAN_II,
            device_index=0,
            can_index=0,
            timing0=TIMING0_250K,
            timing1=TIMING1_250K,
        )

        device = GCANDevice(config)

        try:
            print("Opening GCAN / USBCAN device...")
            device.open()
            print("Device opened")

            while True:
                source_address = SOURCE_ADDRESS

                ccvs_id = j1939_id(
                    PRIORITY_DEFAULT,
                    PGN_CCVS,
                    source_address,
                )
                ccvs_data = build_ccvs_data(
                    speed_kmh=50.0,
                    brake_active=False,
                )

                etc2_id = j1939_id(
                    PRIORITY_DEFAULT,
                    PGN_ETC2,
                    source_address,
                )
                etc2_data = build_etc2_data("Drive")

                oel_id = j1939_id(
                    PRIORITY_OEL,
                    PGN_OEL,
                    source_address,
                )
                oel_data = build_oel_data(
                    left_active=True,
                    right_active=False,
                )

                frames = [
                    (ccvs_id, ccvs_data),
                    (etc2_id, etc2_data),
                    (oel_id, oel_data),
                ]

                for frame_id, data in frames:
                    sent = device.send(frame_id, data)

                    print(
                        f"Sent={sent} "
                        f"ID=0x{frame_id:08X} "
                        f"DATA={' '.join(f'{byte:02X}' for byte in data)}"
                    )

                time.sleep(0.250)

        except KeyboardInterrupt:
            print("Stopped by user")

        finally:
            print("Closing device...")
            device.close()


    if __name__ == "__main__":
        main()

---

## 6. How to run

Open Command Prompt or PowerShell:

    cd C:\path\to\folder
    python gcan_j1939_example.py

Example output:

    Opening GCAN / USBCAN device...
    Device opened
    Sent=1 ID=0x18FEF100 DATA=00 00 32 00 00 00 00 00
    Sent=1 ID=0x18F00500 DATA=00 00 00 7E 00 00 00 00
    Sent=1 ID=0x0CFDCC00 DATA=00 01 00 00 00 00 00 00

---

## 7. Changing the DLL path

If `ECanVci.dll` is in the same directory as the script, use:

    DLL_PATH = "ECanVci.dll"

If it is somewhere else, use an absolute path:

    DLL_PATH = r"C:\GCAN\ECanVci.dll"

---

## 8. Changing the CAN channel

For a dual-channel USBCAN-II adapter:

    CAN_INDEX = 0

usually means CAN channel 1.

    CAN_INDEX = 1

usually means CAN channel 2.

---

## 9. Changing the device type

For USBCAN-II:

    DEVICE_TYPE = 4

For USBCAN-I:

    DEVICE_TYPE = 3

---

## 10. Changing the baud rate

This simulator uses J1939 at 250 kbps:

    TIMING0_250K = 0x01
    TIMING1_250K = 0x1C

Keep these values for normal 250 kbps J1939/FMS use.

Common GCAN timing values from the interface documentation include:

| Baud rate | Timing0 | Timing1 |
|---:|---:|---:|
| 125 kbps | `0x03` | `0x1C` |
| 250 kbps | `0x01` | `0x1C` |
| 500 kbps | `0x00` | `0x1C` |
| 1000 kbps | `0x00` | `0x14` |

---

## 11. Troubleshooting

### `ctypes.WinDLL` fails to load the DLL

Check:

1. You are running on Windows.
2. `ECanVci.dll` exists at `DLL_PATH`.
3. Python bitness matches the DLL bitness.
4. The GCAN driver is installed.

### `OpenDevice failed`

Check:

1. USB-CAN adapter is plugged in.
2. Windows Device Manager sees the adapter.
3. Correct driver is installed.
4. Correct `DEVICE_TYPE` is used.
5. Correct `DEVICE_INDEX` is used.
6. No other program is already using the adapter.

### `InitCAN failed`

Check:

1. Correct CAN channel index.
2. Correct timing values.
3. Adapter supports the selected mode.

### `StartCAN failed`

Check:

1. The channel initialized correctly.
2. Device is not in an error state.
3. Try unplugging/replugging the adapter.

### `Sent=0`

The DLL did not accept the frame for transmission.

Check:

1. Device is open.
2. CAN channel is started.
3. Bus wiring is correct.
4. Bus is terminated.
5. Baud rate matches the rest of the network.
6. The adapter is not bus-off.

---

## 12. Summary

Minimum working flow:

    device = GCANDevice(DeviceConfig())
    device.open()

    frame_id = j1939_id(6, 0x00FEF1, 0x00)
    data = build_ccvs_data(speed_kmh=50.0, brake_active=False)

    sent = device.send(frame_id, data)
    print(sent)

    device.close()

Key points:

- Use `ECanVci.dll`.
- Use `DEVICE_TYPE = 4` for USBCAN-II.
- Use `Timing0 = 0x01`, `Timing1 = 0x1C` for 250 kbps.
- Use `ExternFlag = 1` for J1939 29-bit extended frames.
- Use `Transmit(...)` to send frames.