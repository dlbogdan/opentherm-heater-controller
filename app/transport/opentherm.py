"""Compact OpenTherm frame encoding and decoding helpers."""

MESSAGE_TYPES = (
    "read_data", "write_data", "invalid_data", "reserved",
    "read_ack", "write_ack", "data_invalid", "unknown_data",
)

DATA_NAMES = {
    0: "status",
    1: "control_setpoint",
    5: "application_fault_flags",
    14: "maximum_relative_modulation",
    16: "room_setpoint",
    17: "relative_modulation",
    18: "water_pressure",
    24: "room_temperature",
    25: "boiler_flow_temperature",
    26: "dhw_temperature",
    27: "outside_temperature",
    28: "return_water_temperature",
    56: "dhw_setpoint",
    57: "maximum_boiler_setpoint",
}

F88_DATA_IDS = (1, 14, 16, 17, 18, 24, 25, 26, 27, 28, 56, 57)


def _ones(value):
    count = 0
    while value:
        count += value & 1
        value >>= 1
    return count


def parity_ok(frame):
    """OpenTherm uses odd parity across all 32 bits."""
    return (_ones(frame & 0xFFFFFFFF) & 1) == 1


def f88_encode(value):
    raw = int(round(float(value) * 256.0))
    return raw & 0xFFFF


def f88_decode(raw):
    raw &= 0xFFFF
    if raw & 0x8000:
        raw -= 0x10000
    return raw / 256.0


def build_frame(message_type, data_id, data_value):
    body = ((int(message_type) & 0x7) << 28) | ((int(data_id) & 0xFF) << 16)
    body |= int(data_value) & 0xFFFF
    if (_ones(body) & 1) == 0:
        body |= 0x80000000
    return body


def decode_frame(frame):
    frame = int(frame) & 0xFFFFFFFF
    message_type = (frame >> 28) & 0x7
    data_id = (frame >> 16) & 0xFF
    raw_value = frame & 0xFFFF
    result = {
        "raw": "0x%08X" % frame,
        "parity_ok": parity_ok(frame),
        "message_type": MESSAGE_TYPES[message_type],
        "data_id": data_id,
        "name": DATA_NAMES.get(data_id, "data_id_%d" % data_id),
        "raw_value": raw_value,
    }
    if data_id in F88_DATA_IDS:
        result["value"] = f88_decode(raw_value)
    else:
        result["hb"] = (raw_value >> 8) & 0xFF
        result["lb"] = raw_value & 0xFF
    return result
