import asyncio
import contextvars
import io
import threading
import unittest
import wave
from types import SimpleNamespace

from llm_gateway_core.api.v1 import audio_adapters
from tests._async_compat import run_async


def _build_wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00\x00" * 160)
    return buffer.getvalue()


class _FakeRivaResponse:
    results = []


def _base_transcribe_kwargs(audio_bytes: bytes) -> dict:
    return {
        "request_payload": {
            "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
            "response_format": "json",
        },
        "files_payload": [("file", ("sample.audio", audio_bytes, "application/octet-stream"))],
        "provider_name": "nvidia",
        "provider_base_url": "https://speech.example:50051",
        "provider_api_key": "NVIDIA-KEY",
        "route_custom_headers": {},
        "target_path": "/audio/transcriptions",
    }


def test_non_wav_ffmpeg_conversion_runs_in_to_thread_and_event_loop_progresses(monkeypatch):
    wav_bytes = _build_wav_bytes()
    in_to_thread = contextvars.ContextVar("in_to_thread", default=False)
    release_ffmpeg = threading.Event()
    state = {
        "subprocess_started": False,
        "subprocess_saw_loop_tick": False,
        "subprocess_was_in_to_thread": False,
        "loop_ticks": 0,
    }
    to_thread_funcs = []
    original_to_thread = asyncio.to_thread

    async def tracing_to_thread(func, /, *args, **kwargs):
        to_thread_funcs.append(func)
        token = in_to_thread.set(True)
        try:
            return await original_to_thread(func, *args, **kwargs)
        finally:
            in_to_thread.reset(token)

    def fake_subprocess_run(*_args, **_kwargs):
        state["subprocess_started"] = True
        state["subprocess_was_in_to_thread"] = in_to_thread.get()
        release_ffmpeg.wait(timeout=1.0)
        state["subprocess_saw_loop_tick"] = state["loop_ticks"] > 0
        return SimpleNamespace(returncode=0, stdout=wav_bytes, stderr=b"")

    def fake_call_nvidia_riva_grpc_sync(**_kwargs):
        return _FakeRivaResponse()

    async def loop_probe():
        while not state["subprocess_started"]:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        state["loop_ticks"] += 1
        release_ffmpeg.set()

    async def scenario():
        probe_task = asyncio.create_task(loop_probe())
        response = await audio_adapters.transcribe_with_nvidia_riva_grpc(
            **_base_transcribe_kwargs(b"not-wav-audio")
        )
        await probe_task
        return response

    monkeypatch.setattr(audio_adapters.asyncio, "to_thread", tracing_to_thread)
    monkeypatch.setattr(audio_adapters.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        audio_adapters,
        "_call_nvidia_riva_grpc_sync",
        fake_call_nvidia_riva_grpc_sync,
    )

    response = run_async(asyncio.wait_for(scenario(), timeout=2.0))

    assert response.body == {"text": ""}
    assert audio_adapters._normalize_audio_bytes_for_nvidia in to_thread_funcs
    assert state["subprocess_was_in_to_thread"] is True
    assert state["subprocess_saw_loop_tick"] is True


def test_wav_input_keeps_fast_path_without_ffmpeg_or_normalization_thread(monkeypatch):
    wav_bytes = _build_wav_bytes()
    to_thread_funcs = []
    original_to_thread = asyncio.to_thread

    async def tracing_to_thread(func, /, *args, **kwargs):
        to_thread_funcs.append(func)
        return await original_to_thread(func, *args, **kwargs)

    def fail_if_ffmpeg_runs(*_args, **_kwargs):
        raise AssertionError("WAV input must not invoke ffmpeg conversion")

    def fake_call_nvidia_riva_grpc_sync(**kwargs):
        assert kwargs["audio_bytes"] == wav_bytes
        return _FakeRivaResponse()

    monkeypatch.setattr(audio_adapters.asyncio, "to_thread", tracing_to_thread)
    monkeypatch.setattr(audio_adapters.subprocess, "run", fail_if_ffmpeg_runs)
    monkeypatch.setattr(
        audio_adapters,
        "_call_nvidia_riva_grpc_sync",
        fake_call_nvidia_riva_grpc_sync,
    )

    response = run_async(
        audio_adapters.transcribe_with_nvidia_riva_grpc(**_base_transcribe_kwargs(wav_bytes))
    )

    assert response.body == {"text": ""}
    assert audio_adapters._normalize_audio_bytes_for_nvidia not in to_thread_funcs
    assert audio_adapters._call_nvidia_riva_grpc_sync in to_thread_funcs


if __name__ == "__main__":
    unittest.main()
