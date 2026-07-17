import asyncio
import io
import tempfile
import unittest
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1.audio import _parse_audio_transcription_request
from llm_gateway_core.api.v1.images import _parse_multipart_edit_request
from llm_gateway_core.api.v1.operation_runtime import (
    UPLOAD_HEADER_MAX_BYTES,
    ValidatedUpload,
    admit_upload_processing,
    validate_upload_limited,
    validate_upload_total,
)
from llm_gateway_core.api.v1.pdf import _parse_pdf_multipart_request
from llm_gateway_core.services.upload_admission import UploadAdmission
from tests._async_compat import run_async


_PARSER_CASES = (
    (
        "audio",
        _parse_audio_transcription_request,
        "file",
        "sample.wav",
        b"RIFF\x24\x00\x00\x00WAVEfmt " + (b"\x00" * 16),
        "audio/wav",
        (("model", "gateway/audio"),),
    ),
    (
        "images",
        _parse_multipart_edit_request,
        "image",
        "source.png",
        b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16),
        "image/png",
        (("model", "gateway/image"), ("prompt", "Edit")),
    ),
    (
        "pdf",
        _parse_pdf_multipart_request,
        "file",
        "document.pdf",
        b"%PDF-1.7\n" + (b"\x00" * 16),
        "application/pdf",
        (("model", "gateway/pdf"),),
    ),
)


def _parsed_upload(case_name, parsed):
    if case_name == "images":
        return parsed[1]["images"][0]
    return parsed[2]


def _multipart_parts(case, extra_fields=()):
    _, _, field_name, filename, content, content_type, required_fields = case
    return [
        (field_name, (filename, content, content_type)),
        *((name, (None, value)) for name, value in required_fields),
        *((name, (None, value)) for name, value in extra_fields),
    ]


def _parser_app(parser, *, on_parsed=None, terminal_error=False):
    app = FastAPI()

    @app.post("/")
    async def parse_multipart(request: Request):
        async with parser(request) as parsed:
            if on_parsed is not None:
                on_parsed(parsed)
            if terminal_error:
                raise HTTPException(status_code=418, detail="terminal test error")
            return {"ok": True}

    return app


def _starlette_multipart_request(parts):
    encoded_request = httpx.Request("POST", "http://test/", files=parts)
    body = encoded_request.read()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [
            (name.lower(), value)
            for name, value in encoded_request.headers.raw
        ],
        "client": ("testclient", 50000),
        "server": ("test", 80),
        "app": FastAPI(),
    }
    return Request(scope, receive)


class _FakeUploadFile:
    def __init__(
        self,
        content: bytes,
        *,
        filename: str = "sample.wav",
        content_type: str = "audio/wav",
        size: int | None = None,
    ):
        self.file = io.BytesIO(content)
        self.filename = filename
        self.content_type = content_type
        self.size = len(content) if size is None else size
        self.read_sizes: list[int] = []
        self.seek_offsets: list[int] = []

    async def read(self, size: int = -1):
        self.read_sizes.append(size)
        return self.file.read(size)

    async def seek(self, offset: int):
        self.seek_offsets.append(offset)
        return self.file.seek(offset)


class OperationRuntimeUploadTests(unittest.TestCase):
    def test_validate_upload_limited_rejects_file_larger_than_limit(self):
        upload = _FakeUploadFile(
            b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32,
        )

        with self.assertRaises(HTTPException) as ctx:
            run_async(
                validate_upload_limited(
                    upload,
                    default_filename="audio.bin",
                    kind="audio",
                    max_bytes=16,
                )
            )

        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(ctx.exception.detail, "Uploaded file too large.")

    def test_validate_upload_limited_accepts_valid_png_with_generic_content_type(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        upload = _FakeUploadFile(
            png_bytes,
            filename="source.png",
            content_type="application/octet-stream",
        )

        validated = run_async(
            validate_upload_limited(
                upload,
                default_filename="upload.bin",
                kind="image",
                max_bytes=64,
            )
        )

        self.assertEqual(validated.filename, "source.png")
        self.assertIs(validated.file, upload.file)
        self.assertEqual(validated.content_type, "application/octet-stream")

    def test_validate_upload_limited_accepts_mp4_audio_with_video_mp4_content_type(self):
        mp4_bytes = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 8
        upload = _FakeUploadFile(
            mp4_bytes,
            filename="sample.m4a",
            content_type="video/mp4",
        )

        validated = run_async(
            validate_upload_limited(
                upload,
                default_filename="audio.bin",
                kind="audio",
                max_bytes=64,
            )
        )

        self.assertEqual(validated.filename, "sample.m4a")
        self.assertIs(validated.file, upload.file)
        self.assertEqual(validated.content_type, "video/mp4")

    def test_validate_upload_limited_accepts_ogg_audio_with_application_ogg_content_type(self):
        ogg_bytes = b"OggS" + b"\x00" * 16
        upload = _FakeUploadFile(
            ogg_bytes,
            filename="sample.ogg",
            content_type="application/ogg",
        )

        validated = run_async(
            validate_upload_limited(
                upload,
                default_filename="audio.bin",
                kind="audio",
                max_bytes=64,
            )
        )

        self.assertEqual(validated.filename, "sample.ogg")
        self.assertIs(validated.file, upload.file)
        self.assertEqual(validated.content_type, "application/ogg")

    def test_known_size_validated_upload_reads_only_header_and_rewinds(self):
        content = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"x" * 128
        upload = _FakeUploadFile(content)

        validated = run_async(
            validate_upload_limited(
                upload,
                default_filename="audio.bin",
                kind="audio",
                max_bytes=len(content),
            )
        )

        self.assertIsInstance(validated, ValidatedUpload)
        self.assertIs(validated.file, upload.file)
        self.assertEqual(validated.size, len(content))
        self.assertEqual(upload.read_sizes, [UPLOAD_HEADER_MAX_BYTES])
        self.assertEqual(upload.file.tell(), 0)

    def test_unknown_size_scans_without_accumulating_and_rewinds(self):
        content = b"%PDF-1.7\n" + b"x" * 128
        upload = _FakeUploadFile(
            content,
            filename="document.pdf",
            content_type="application/pdf",
        )
        upload.size = None

        validated = run_async(
            validate_upload_limited(
                upload,
                default_filename="document.pdf",
                kind="pdf",
                max_bytes=len(content),
            )
        )

        self.assertEqual(validated.size, len(content))
        self.assertNotIn(-1, upload.read_sizes)
        self.assertEqual(upload.file.tell(), 0)

    def test_unknown_size_overflow_rewinds_before_error(self):
        upload = _FakeUploadFile(b"%PDF-1.7\n" + b"x" * 32)
        upload.size = None

        with self.assertRaises(HTTPException) as ctx:
            run_async(
                validate_upload_limited(
                    upload,
                    default_filename="document.pdf",
                    kind="pdf",
                    max_bytes=8,
                )
            )

        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(upload.file.tell(), 0)

    def test_upload_total_accepts_limit_and_rejects_limit_plus_one(self):
        first = ValidatedUpload("first.png", "image/png", 4, io.BytesIO())
        second = ValidatedUpload("second.png", "image/png", 5, io.BytesIO())

        self.assertEqual(validate_upload_total([first, second], max_bytes=9), 9)
        with self.assertRaises(HTTPException) as ctx:
            validate_upload_total([first, second], max_bytes=8)

        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(ctx.exception.detail, "Uploaded files total too large.")

    def test_admission_context_releases_and_maps_local_capacity_errors(self):
        async def scenario() -> None:
            admission = UploadAdmission(max_bytes=4)
            request = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        services=SimpleNamespace(
                            upload_admission=admission,
                            upload_admission_timeout_seconds=0.01,
                        )
                    )
                )
            )

            async with admit_upload_processing(request, 4):
                self.assertEqual(admission.snapshot.active_bytes, 4)
            self.assertEqual(admission.snapshot.active_bytes, 0)

            with self.assertRaises(HTTPException) as oversized:
                async with admit_upload_processing(request, 5):
                    self.fail("oversized admission unexpectedly succeeded")
            self.assertEqual(oversized.exception.status_code, 413)

            active = await admission.acquire(4)
            with self.assertRaises(HTTPException) as unavailable:
                async with admit_upload_processing(request, 1):
                    self.fail("timed-out admission unexpectedly succeeded")
            self.assertEqual(unavailable.exception.status_code, 503)
            active.release()
            self.assertEqual(admission.snapshot.active_bytes, 0)
            self.assertEqual(admission.snapshot.waiters, 0)

        run_async(scenario())


class MultipartParserContractTests(unittest.TestCase):
    def test_scalar_field_count_and_part_size_limits(self):
        expected_details = {
            "field_count": "Too many fields. Maximum number of fields is 64.",
            "part_size": "Part exceeded maximum size of 64KB.",
        }

        for case in _PARSER_CASES:
            case_name, parser, *_, required_fields = case
            field_count_extras = tuple(
                (f"extra_{index}", "x")
                for index in range(65 - len(required_fields))
            )
            attempts = (
                ("field_count", field_count_extras),
                ("part_size", (("oversized", "x" * ((64 * 1024) + 1)),)),
            )
            for limit_name, extra_fields in attempts:
                with self.subTest(parser=case_name, limit=limit_name):
                    with TestClient(_parser_app(parser)) as client:
                        response = client.post(
                            "/",
                            files=_multipart_parts(case, extra_fields),
                        )

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(
                        response.json()["detail"],
                        expected_details[limit_name],
                    )

    def test_real_spools_close_after_terminal_error(self):
        for case in _PARSER_CASES:
            case_name, parser, *_ = case
            captured_files = []

            def capture(parsed):
                captured_files.append(_parsed_upload(case_name, parsed).file)

            with self.subTest(parser=case_name):
                with TestClient(
                    _parser_app(
                        parser,
                        on_parsed=capture,
                        terminal_error=True,
                    )
                ) as client:
                    response = client.post("/", files=_multipart_parts(case))

                self.assertEqual(response.status_code, 418)
                self.assertEqual(len(captured_files), 1)
                self.assertIsInstance(
                    captured_files[0],
                    tempfile.SpooledTemporaryFile,
                )
                self.assertTrue(captured_files[0].closed)

    def test_real_spools_close_before_cancellation_propagates(self):
        async def scenario() -> None:
            for case in _PARSER_CASES:
                case_name, parser, *_ = case
                request = _starlette_multipart_request(_multipart_parts(case))
                entered = asyncio.Event()
                blocker = asyncio.Event()
                captured_files = []

                async def parse_until_cancelled():
                    async with parser(request) as parsed:
                        captured_files.append(_parsed_upload(case_name, parsed).file)
                        entered.set()
                        await blocker.wait()

                task = asyncio.create_task(parse_until_cancelled())
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                self.assertEqual(len(captured_files), 1, case_name)
                self.assertIsInstance(
                    captured_files[0],
                    tempfile.SpooledTemporaryFile,
                    case_name,
                )
                self.assertTrue(captured_files[0].closed, case_name)

        run_async(scenario())


if __name__ == "__main__":
    unittest.main()
