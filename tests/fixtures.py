"""Small synthetic fixtures; no game data is distributed with the tests."""
import plistlib
import struct


def executable():
    data = bytearray(0x2100)
    struct.pack_into(">2I", data, 0, 0xcafebabe, 2)
    struct.pack_into(">5I", data, 8, 0x01000007, 3, 0x1000, 0x400, 12)
    struct.pack_into(">5I", data, 28, 7, 3, 0x2000, 0x100, 12)
    base = 0x1000
    struct.pack_into("<8I", data, base, 0xfeedfacf, 0x01000007, 3, 2, 1, 152, 0, 0)
    struct.pack_into("<2I16s4Q4I", data, base + 32,
                     0x19, 152, b"__TEXT", 0x100000000, 0x400, 0, 0x400, 7, 5, 1, 0)
    struct.pack_into("<16s16s2Q8I", data, base + 104,
                     b"__text", b"__TEXT", 0x100000200, 0x200, 0x200, 2, 0, 0, 0x80000400, 0, 0, 0)
    data[base + 0x200:base + 0x400] = bytes((i * 19 + 7) % 256 for i in range(0x200))
    data[0x2000:0x2100] = bytes((i * 31 + 9) % 256 for i in range(0x100))
    return bytes(data)


def settings():
    player = bytearray((i * 37 + 11) % 256 for i in range(337))
    player[0x80] = 1
    player[0xa6] = 0
    player[0xb0:0xb4] = bytes(4)
    metadata = bytearray(b"5.3.6p1\0" + struct.pack("<iB", 2, 0))
    metadata += struct.pack("<i", 2)
    metadata += struct.pack("<i16s", 129, bytes(16))
    metadata += struct.pack("<i16s", 1, bytes(16))
    metadata += struct.pack("<i", 2)
    for path_id, offset, size, type_id, class_id in ((1, 0, 337, 0, 129), (2, 337, 4, 1, 1)):
        metadata += bytes((-(20 + len(metadata))) % 4)
        metadata += struct.pack("<qIIihhB", path_id, offset, size, type_id, class_id, 0, 0)
    data_offset = (20 + len(metadata) + 15) & ~15
    data = bytearray(data_offset + len(player) + 4)
    struct.pack_into(">4I", data, 0, len(metadata), len(data), 15, data_offset)
    data[20:20 + len(metadata)] = metadata
    data[data_offset:data_offset + len(player)] = player
    data[-4:] = b"KEEP"
    return bytes(data), bytes(player)


def info():
    return plistlib.dumps(dict(CFBundleExecutable="EndlessLegend", UnityBuildNumber="ef59fdfbd254",
                               CFBundleIdentifier="example.synthetic.game", OtherSetting="keep-me"))


def native(with_uuid=True):
    # Minimal stand-in for filesystem tests; never executed or installed live.
    commands = struct.pack("<2I16s", 0x1b, 24, bytes(range(16))) if with_uuid else b""
    return struct.pack("<8I", 0xfeedfacf, 0x01000007, 3, 6,
                       int(with_uuid), len(commands), 0, 0) + commands
