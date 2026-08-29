"""The YouTube id parser behind the writers' trailer field (audit R3)."""

import pytest

from kofin.sync.fields import youtube_video_id


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://example.com/trailer.mp4", None),
        ("https://www.youtube.com/", None),
        ("", None),
        (None, None),
    ],
)
def test_youtube_video_id(url, expected):
    assert youtube_video_id(url) == expected
