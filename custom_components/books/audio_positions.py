"""Audiobook positions between the tolino cloud and Audiobookshelf.

tolino: "#medialoc(TRACK,SECONDS)" - the track (counted from 1) and the whole seconds into that track (verified on a real account: pausing at 0:30
of track 1 gave (1,31), at about 1:09 of track 2 gave (2,67)). Audiobookshelf: `currentTime`, the seconds from the start of the whole audiobook.
The two meet through the lengths of the tracks, which are the lengths of the files of the audiobookshelf item (the same MP3s the import loaded).

tolino's `progress` is not needed to find the place (the position says it all) but is written along for the library's percentage. Measured on 11
bookmarks of the tolino app: progress * overallDuration = seconds played + about 20 s, where overallDuration (whole minutes, 1440 s) is a bit more
than the sum of the tracks (1420 s); so progress = (played + (overall - sum of the tracks)) / overall."""
from __future__ import annotations

import re

_MEDIALOC = re.compile(r"^#medialoc\((\d{1,3}),(\d{1,6})\)$")


def medialoc_to_time(position: str | None, durations: list[float]) -> float | None:
    """Audiobookshelf's currentTime for a tolino position; None when the position does not fit the audiobook."""
    match = _MEDIALOC.match(str(position or ""))
    if not match or not durations:
        return None
    track, seconds = int(match.group(1)), int(match.group(2))
    if not 1 <= track <= len(durations):
        return None
    return sum(durations[: track - 1]) + min(float(seconds), durations[track - 1])


def time_to_medialoc(current_time: float, durations: list[float]) -> str | None:
    """The tolino position for Audiobookshelf's currentTime (whole seconds, the last track holds everything beyond the end)."""
    if not durations or current_time < 0:
        return None
    remaining = float(current_time)
    for index, length in enumerate(durations):
        if remaining < length or index == len(durations) - 1:
            return f"#medialoc({index + 1},{int(min(remaining, length))})"
        remaining -= length
    return None


def tolino_progress(played: float, track_ms: list[int], overall_s: float) -> float:
    """The 0..1 value the tolino app writes next to a position (see the module text)."""
    overall = float(overall_s or 0)
    if overall <= 0:
        return 0.0
    slack = max(0.0, overall - sum(track_ms) / 1000)
    return max(0.0, min(1.0, (played + slack) / overall))
