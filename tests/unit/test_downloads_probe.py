"""L1 units for the transcode completion probes (plan W3.1/W3.2).

The fixtures are hand-built boxes/pages, byte-exact per ISO 14496-12 and RFC
3533 — no ffmpeg dependency, and the truncation cases are cut at chosen
byte boundaries, which a generated file could not guarantee.
"""

import struct

from kofin.downloads import probe

# -- fMP4 builders -------------------------------------------------------------


def box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def fullbox(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(box_type, bytes([version]) + flags.to_bytes(3, "big") + payload)


def tkhd(track_id: int) -> bytes:
    return fullbox(b"tkhd", 0, 0, struct.pack(">III", 0, 0, track_id))


def mdhd(timescale: int, duration: int) -> bytes:
    return fullbox(b"mdhd", 0, 0, struct.pack(">IIII", 0, 0, timescale, duration))


def trak(track_id: int, timescale: int, duration: int = 0) -> bytes:
    return box(b"trak", tkhd(track_id) + box(b"mdia", mdhd(timescale, duration)))


def trex(track_id: int, default_duration: int) -> bytes:
    return fullbox(
        b"trex", 0, 0, struct.pack(">IIIII", track_id, 1, default_duration, 0, 0)
    )


def moov(*children: bytes) -> bytes:
    return box(b"moov", b"".join(children))


def tfhd(track_id: int, default_duration: int = 0) -> bytes:
    if default_duration:
        return fullbox(
            b"tfhd", 0, 0x000008, struct.pack(">II", track_id, default_duration)
        )
    return fullbox(b"tfhd", 0, 0, struct.pack(">I", track_id))


def trun_counted(sample_count: int) -> bytes:
    return fullbox(b"trun", 0, 0, struct.pack(">I", sample_count))


def trun_durations(durations: list) -> bytes:
    # duration + size present per sample (0x300): the stride case.
    payload = struct.pack(">I", len(durations))
    for duration in durations:
        payload += struct.pack(">II", duration, 999)
    return fullbox(b"trun", 0, 0x000300, payload)


def moof(*trafs: bytes) -> bytes:
    return box(b"moof", b"".join(box(b"traf", traf) for traf in trafs))


FTYP = box(b"ftyp", b"iso5" + b"\x00" * 8)


def test_fragment_durations_sum_via_trex_default(tmp_path):
    # empty_moov: mdhd duration 0; two fragments of 25 samples * 40 units
    # (trex default) at timescale 1000 -> 2.0 s.
    data = (
        FTYP
        + moov(trak(1, 1000), box(b"mvex", trex(1, 40)))
        + moof(tfhd(1) + trun_counted(25))
        + box(b"mdat", b"x" * 64)
        + moof(tfhd(1) + trun_counted(25))
        + box(b"mdat", b"x" * 64)
    )
    path = tmp_path / "a.mp4"
    path.write_bytes(data)
    assert probe.fmp4_duration_seconds(str(path)) == 2.0


def test_per_sample_durations_and_tfhd_default(tmp_path):
    data = (
        FTYP
        + moov(trak(7, 1000))
        + moof(tfhd(7, 40) + trun_durations([500, 500, 500]))
        + box(b"mdat", b"x" * 8)
        + moof(tfhd(7, 40) + trun_counted(10))  # 10 * tfhd default 40
        + box(b"mdat", b"x" * 8)
    )
    path = tmp_path / "b.mp4"
    path.write_bytes(data)
    assert probe.fmp4_duration_seconds(str(path)) == 1.9  # 1500 + 400 units


def test_a_truncated_part_measures_the_whole_fragments(tmp_path):
    """The dead-encoder case: the file ends mid-box, and the probe reports
    what is whole rather than raising — that shortfall is the signal."""
    whole = (
        FTYP
        + moov(trak(1, 1000), box(b"mvex", trex(1, 40)))
        + moof(tfhd(1) + trun_counted(25))
        + box(b"mdat", b"x" * 64)
    )
    second = moof(tfhd(1) + trun_counted(25))
    path = tmp_path / "c.mp4"
    path.write_bytes(whole + second[: len(second) // 2])
    assert probe.fmp4_duration_seconds(str(path)) == 1.0


def test_a_plain_mp4_reads_the_mdhd_duration(tmp_path):
    path = tmp_path / "d.mp4"
    path.write_bytes(FTYP + moov(trak(1, 1000, duration=3000)))
    assert probe.fmp4_duration_seconds(str(path)) == 3.0


def test_two_tracks_answer_the_longer(tmp_path):
    data = (
        FTYP
        + moov(trak(1, 1000), trak(2, 90000))
        + moof(tfhd(1, 40) + trun_counted(25))  # 1.0 s
        + moof(tfhd(2, 3000) + trun_counted(60))  # 2.0 s
    )
    path = tmp_path / "e.mp4"
    path.write_bytes(data)
    assert probe.fmp4_duration_seconds(str(path)) == 2.0


def test_garbage_answers_none(tmp_path):
    path = tmp_path / "f.mp4"
    path.write_bytes(b"not an mp4 at all")
    assert probe.fmp4_duration_seconds(str(path)) is None
    assert probe.fmp4_duration_seconds(str(tmp_path / "missing.mp4")) is None


# -- Ogg/Opus ------------------------------------------------------------------


def ogg_page(granule: int, sequence: int) -> bytes:
    header = b"OggS" + bytes([0, 0]) + struct.pack("<q", granule)
    header += struct.pack("<III", 1, sequence, 0)
    return header + bytes([1, 4]) + b"data"


def test_ogg_duration_reads_the_last_granule(tmp_path):
    path = tmp_path / "a.opus"
    path.write_bytes(ogg_page(96000, 0) + ogg_page(480000, 1))
    assert probe.ogg_duration_seconds(str(path)) == 10.0


def test_ogg_walks_past_a_granuleless_page(tmp_path):
    path = tmp_path / "b.opus"
    path.write_bytes(ogg_page(96000, 0) + ogg_page(-1, 1))
    assert probe.ogg_duration_seconds(str(path)) == 2.0


def test_ogg_garbage_answers_none(tmp_path):
    path = tmp_path / "c.opus"
    path.write_bytes(b"nothing oggish here")
    assert probe.ogg_duration_seconds(str(path)) is None


# -- dispatch ------------------------------------------------------------------


def test_dispatch_by_container(tmp_path):
    mp4 = tmp_path / "x.mp4"
    mp4.write_bytes(FTYP + moov(trak(1, 1000, duration=1000)))
    assert probe.duration_seconds(str(mp4), "mp4") == 1.0
    opus = tmp_path / "x.opus"
    opus.write_bytes(ogg_page(48000, 0))
    assert probe.duration_seconds(str(opus), "opus") == 1.0
    assert probe.duration_seconds(str(mp4), "aac") is None  # no probe for adts
