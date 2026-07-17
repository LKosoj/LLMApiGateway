import pytest

from llm_gateway_core.services.stream_observation import (
    SSEEvent,
    SSEEventTooLarge,
    SSEFramer,
    SSEFramerStateError,
    SSEInvalidEncoding,
    SSEMalformedEvent,
    parse_sse_json,
)


def test_framer_preserves_split_utf8_until_complete_event() -> None:
    framer = SSEFramer()
    encoded = "Привет".encode()

    first = framer.feed(b"data: " + encoded[:1])
    assert first.events == ()
    assert first.consumed_bytes == 0

    second_input = encoded[1:] + b"\n\n"
    second = framer.feed(second_input)
    assert len(second.events) == 1
    assert second.events[0].data == "Привет"
    assert second.events[0].event is None
    assert second.events[0].done is False
    assert second.consumed_bytes == len(b"data: ") + len(encoded) + 2
    assert framer.buffered_bytes == 0


def test_framer_supports_lf_crlf_comments_event_name_and_multiline_data() -> None:
    payload = (
        b": ignored\r\n"
        b"event: delta\r\n"
        b"data: first\r\n"
        b"data: second\r\n"
        b"\r\n"
        b"data: third\n\n"
    )

    batch = SSEFramer().feed(payload)

    assert batch.consumed_bytes == len(payload)
    assert [(event.event, event.data) for event in batch.events] == [
        ("delta", "first\nsecond"),
        (None, "third"),
    ]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_sse_json_parser_rejects_non_finite_constants(constant: str) -> None:
    with pytest.raises(ValueError, match="non-finite JSON constants"):
        parse_sse_json(SSEEvent(f'{{"value":{constant}}}'))


def test_batch_reports_data_less_frames_after_last_data_event() -> None:
    framer = SSEFramer(max_event_bytes=128)
    trailing_frame = b": trailing comment\n\n"

    batch = framer.feed(b"data: stop\n\n" + trailing_frame)

    assert [event.data for event in batch.events] == ["stop"]
    assert batch.trailing_frame_bytes == len(trailing_frame)


def test_framer_reports_only_consumed_prefix_when_tail_is_incomplete() -> None:
    complete = b"data: one\n\n"
    incomplete = b"data: tw"
    framer = SSEFramer()

    batch = framer.feed(complete + incomplete)

    assert [event.data for event in batch.events] == ["one"]
    assert batch.consumed_bytes == len(complete)
    assert framer.buffered_bytes == len(incomplete)


def test_done_is_terminal_and_all_following_bytes_are_consumed_as_noop() -> None:
    payload = b"data: [DONE]\n\ndata: must-not-parse\n\n"
    framer = SSEFramer()

    batch = framer.feed(payload)

    assert len(batch.events) == 1
    assert batch.events[0].data == "[DONE]"
    assert batch.events[0].done is True
    assert batch.consumed_bytes == len(payload)
    assert framer.terminal is True
    assert framer.terminal_trailing_bytes == len(b"data: must-not-parse\n\n")

    ignored = framer.feed(b"data: ignored\n\n")
    assert ignored.events == ()
    assert ignored.consumed_bytes == len(b"data: ignored\n\n")
    assert framer.terminal_trailing_bytes == len(
        b"data: must-not-parse\n\ndata: ignored\n\n"
    )
    assert framer.finish().events == ()


def test_finish_flushes_eof_tail_and_is_idempotent() -> None:
    payload = b"event: tail\ndata: final"
    framer = SSEFramer()
    assert framer.feed(payload).consumed_bytes == 0

    finished = framer.finish()

    assert finished.consumed_bytes == len(payload)
    assert len(finished.events) == 1
    assert finished.events[0].event == "tail"
    assert finished.events[0].data == "final"
    assert framer.finish().consumed_bytes == 0
    with pytest.raises(SSEFramerStateError) as state_error:
        framer.feed(b"data: late\n\n")
    assert state_error.value.reason_code == "framer_finished"


def test_invalid_utf8_error_is_typed_safe_and_abort_releases_retained_bytes() -> None:
    payload = b"data: \xff\n\nprivate-tail"
    framer = SSEFramer()

    with pytest.raises(SSEInvalidEncoding) as error:
        framer.feed(payload)

    assert error.value.reason_code == "invalid_utf8"
    assert "private-tail" not in str(error.value)
    assert "data:" not in str(error.value)
    assert framer.abort() == len(payload)
    assert framer.abort() == 0
    with pytest.raises(SSEFramerStateError) as state_error:
        framer.feed(b"data: later\n\n")
    assert state_error.value.reason_code == "framer_failed"


def test_event_limit_is_inclusive_and_oversize_error_contains_no_payload() -> None:
    allowed = SSEFramer(max_event_bytes=10)
    assert allowed.feed(b"data: 1234").events == ()
    assert allowed.finish().events[0].data == "1234"

    oversized_payload = b"data: SECRET"
    oversized = SSEFramer(max_event_bytes=10)
    with pytest.raises(SSEEventTooLarge) as error:
        oversized.feed(oversized_payload)

    assert error.value.reason_code == "event_too_large"
    assert error.value.max_bytes == 10
    assert "SECRET" not in str(error.value)
    assert oversized.abort() == len(oversized_payload)


def test_crlf_may_be_split_but_bare_carriage_return_is_rejected() -> None:
    valid = SSEFramer()
    first = valid.feed(b"data: value\r")
    assert first.events == ()
    second = valid.feed(b"\n\r\n")
    assert second.events[0].data == "value"

    malformed = SSEFramer()
    with pytest.raises(SSEMalformedEvent) as error:
        malformed.feed(b"data: hidden\rvalue")
    assert error.value.reason_code == "malformed_line_ending"
    assert "hidden" not in str(error.value)
    assert malformed.abort() == len(b"data: hidden\rvalue")


def test_comments_are_ignored_but_empty_data_field_is_an_event() -> None:
    batch = SSEFramer().feed(b": heartbeat\n\ndata:\n\n")

    assert len(batch.events) == 1
    assert batch.events[0].data == ""


def test_unicode_line_separator_inside_data_is_not_an_sse_line_break() -> None:
    payload = 'data: {"text":"a\u2028b"}\n\n'.encode()

    batch = SSEFramer().feed(payload)

    assert len(batch.events) == 1
    assert batch.events[0].data == '{"text":"a\u2028b"}'


def test_abort_discards_retained_tail_and_closes_framer() -> None:
    framer = SSEFramer()
    payload = b"data: pending"
    assert framer.feed(payload).consumed_bytes == 0

    assert framer.abort() == len(payload)
    assert framer.abort() == 0
    with pytest.raises(SSEFramerStateError) as error:
        framer.feed(b"data: late\n\n")
    assert error.value.reason_code == "framer_finished"


@pytest.mark.parametrize("max_event_bytes", [0, -1, True])
def test_framer_rejects_invalid_event_limits(max_event_bytes: int) -> None:
    with pytest.raises(ValueError, match="max_event_bytes"):
        SSEFramer(max_event_bytes=max_event_bytes)


def test_framer_requires_immutable_bytes_input() -> None:
    with pytest.raises(TypeError, match="data must be bytes"):
        SSEFramer().feed(bytearray(b"data: value\n\n"))  # type: ignore[arg-type]


def test_many_events_in_one_chunk_are_consumed_in_order() -> None:
    payload = b"".join(
        f"data: {index}\n\n".encode("ascii")
        for index in range(2_000)
    )
    framer = SSEFramer(max_event_bytes=32)

    batch = framer.feed(payload)

    assert batch.consumed_bytes == len(payload)
    assert len(batch.events) == 2_000
    assert batch.events[0].data == "0"
    assert batch.events[-1].data == "1999"
    assert framer.buffered_bytes == 0
