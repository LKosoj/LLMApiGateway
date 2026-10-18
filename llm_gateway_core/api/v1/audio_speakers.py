"""Merge timestamped transcription segments with diarization speaker turns."""

from collections.abc import Mapping

from fastapi import HTTPException


def _segment_bounds(item: object, source: str) -> tuple[float, float]:
    if not isinstance(item, Mapping):
        raise HTTPException(status_code=502, detail=f"{source} returned a malformed segment.")
    start = item.get("start")
    end = item.get("end")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
    ):
        raise HTTPException(status_code=502, detail=f"{source} returned a segment without numeric start/end.")
    return float(start), float(end)


def _speaker_turns(diarization: Mapping[str, object]) -> list[tuple[float, float, str]]:
    raw_turns = diarization.get("segments")
    if not isinstance(raw_turns, list):
        raise HTTPException(status_code=502, detail="Diarization model returned no 'segments' list.")
    turns: list[tuple[float, float, str]] = []
    for item in raw_turns:
        start, end = _segment_bounds(item, "Diarization model")
        speaker = item.get("speaker")
        if not isinstance(speaker, str) or not speaker:
            raise HTTPException(status_code=502, detail="Diarization model returned a segment without 'speaker'.")
        turns.append((start, end, speaker))
    return turns


def _pick_speaker(start: float, end: float, turns: list[tuple[float, float, str]]) -> str | None:
    """Speaker with the largest time overlap; the nearest turn when nothing overlaps."""
    overlaps: dict[str, float] = {}
    for turn_start, turn_end, speaker in turns:
        overlap = min(end, turn_end) - max(start, turn_start)
        if overlap > 0:
            overlaps[speaker] = overlaps.get(speaker, 0.0) + overlap
    if overlaps:
        return max(overlaps, key=lambda speaker: overlaps[speaker])

    nearest_speaker: str | None = None
    nearest_gap = float("inf")
    for turn_start, turn_end, speaker in turns:
        gap = max(turn_start - end, start - turn_end, 0.0)
        if gap < nearest_gap:
            nearest_gap = gap
            nearest_speaker = speaker
    return nearest_speaker


def merge_transcription_with_diarization(
    transcription: Mapping[str, object],
    diarization: Mapping[str, object],
) -> dict[str, object]:
    raw_segments = transcription.get("segments")
    if not isinstance(raw_segments, list):
        raise HTTPException(
            status_code=502,
            detail="Transcription model returned no timestamped 'segments'; speaker split needs verbose_json segments.",
        )
    turns = _speaker_turns(diarization)

    segments: list[dict[str, object]] = []
    for item in raw_segments:
        start, end = _segment_bounds(item, "Transcription model")
        text = item.get("text")
        segments.append(
            {
                "id": len(segments),
                "start": start,
                "end": end,
                "speaker": _pick_speaker(start, end, turns),
                "text": text.strip() if isinstance(text, str) else "",
            }
        )

    lines: list[tuple[str | None, list[str]]] = []
    for segment in segments:
        if not segment["text"]:
            continue
        if lines and lines[-1][0] == segment["speaker"]:
            lines[-1][1].append(str(segment["text"]))
        else:
            lines.append((segment["speaker"], [str(segment["text"])]))

    result: dict[str, object] = {
        "text": "\n".join(f"{speaker or 'UNKNOWN'}: {' '.join(parts)}" for speaker, parts in lines),
        "num_speakers": len({speaker for _start, _end, speaker in turns}),
        "segments": segments,
    }
    duration = transcription.get("duration", diarization.get("duration"))
    if duration is not None:
        result["duration"] = duration
    language = transcription.get("language")
    if language is not None:
        result["language"] = language
    return result
