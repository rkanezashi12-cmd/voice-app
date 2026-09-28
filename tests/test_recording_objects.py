from __future__ import annotations

from app import recording_objects as ro


def test_parse_and_group_in_recording_order() -> None:
    names = [
        "recordings/default/r1/1727488600000-a-zzzz99/000000.m4a",
        "recordings/default/r1/1727488500123-a-abc123/000001.m4a",
        "recordings/default/r1/1727488500123-a-abc123/000000.m4a",
        "recordings/default/r1/1727488700000-b-qqqq11/000000.webm",
        "recordings/default/r1/bad-session/000000.m4a",
        "recordings/default/r1/1727488500123-a-abc123/not-a-chunk.txt",
    ]
    chunks = [c for c in (ro.parse_object(n) for n in names) if c]
    assert len(chunks) == 4
    sessions = ro.group_sessions(chunks)
    assert [s.session_id for s in sessions] == [
        "1727488500123-a-abc123",
        "1727488600000-a-zzzz99",
        "1727488700000-b-qqqq11",
    ]
    assert [c.seq for c in sessions[0].chunks] == [0, 1]
    assert sessions[2].mode == "b"


def test_mime_mapping() -> None:
    assert ro.base_mime("audio/webm;codecs=opus") == "audio/webm"
    assert ro.MIME_EXTENSIONS[ro.base_mime("audio/mp4; codecs=mp4a.40.2")] == "m4a"
    assert ro.object_name("default", "r1", "1727488500123-a-abc123", 7, "webm").endswith("/000007.webm")
