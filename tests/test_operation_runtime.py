import unittest

from fastapi import HTTPException

from llm_gateway_core.api.v1.operation_runtime import serialize_upload_limited
from tests._async_compat import run_async


class _FakeUploadFile:
    def __init__(self, chunks, *, filename: str = "sample.wav", content_type: str = "audio/wav"):
        self._chunks = list(chunks)
        self.filename = filename
        self.content_type = content_type

    async def read(self, _size: int = -1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class OperationRuntimeUploadTests(unittest.TestCase):
    def test_serialize_upload_limited_rejects_file_larger_than_limit(self):
        upload = _FakeUploadFile(
            [
                b"RIFF\x24\x00\x00\x00WAVEfmt ",
                b"\x00" * 32,
            ]
        )

        with self.assertRaises(HTTPException) as ctx:
            run_async(
                serialize_upload_limited(
                    upload,
                    default_filename="audio.bin",
                    kind="audio",
                    max_bytes=16,
                )
            )

        self.assertEqual(ctx.exception.status_code, 413)
        self.assertEqual(ctx.exception.detail, "Uploaded file too large.")

    def test_serialize_upload_limited_accepts_valid_file_at_limit(self):
        upload = _FakeUploadFile(
            [b"RIFF\x24\x00\x00\x00WAVEfmt "],
            filename="sample.wav",
            content_type="audio/wav",
        )

        filename, content, content_type = run_async(
            serialize_upload_limited(
                upload,
                default_filename="audio.bin",
                kind="audio",
                max_bytes=16,
            )
        )

        self.assertEqual(filename, "sample.wav")
        self.assertEqual(content, b"RIFF\x24\x00\x00\x00WAVEfmt ")
        self.assertEqual(content_type, "audio/wav")

    def test_serialize_upload_limited_accepts_valid_png_with_generic_content_type(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        upload = _FakeUploadFile(
            [png_bytes],
            filename="source.png",
            content_type="application/octet-stream",
        )

        filename, content, content_type = run_async(
            serialize_upload_limited(
                upload,
                default_filename="upload.bin",
                kind="image",
                max_bytes=64,
            )
        )

        self.assertEqual(filename, "source.png")
        self.assertEqual(content, png_bytes)
        self.assertEqual(content_type, "application/octet-stream")

    def test_serialize_upload_limited_accepts_mp4_audio_with_video_mp4_content_type(self):
        mp4_bytes = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 8
        upload = _FakeUploadFile(
            [mp4_bytes],
            filename="sample.m4a",
            content_type="video/mp4",
        )

        filename, content, content_type = run_async(
            serialize_upload_limited(
                upload,
                default_filename="audio.bin",
                kind="audio",
                max_bytes=64,
            )
        )

        self.assertEqual(filename, "sample.m4a")
        self.assertEqual(content, mp4_bytes)
        self.assertEqual(content_type, "video/mp4")

    def test_serialize_upload_limited_accepts_ogg_audio_with_application_ogg_content_type(self):
        ogg_bytes = b"OggS" + b"\x00" * 16
        upload = _FakeUploadFile(
            [ogg_bytes],
            filename="sample.ogg",
            content_type="application/ogg",
        )

        filename, content, content_type = run_async(
            serialize_upload_limited(
                upload,
                default_filename="audio.bin",
                kind="audio",
                max_bytes=64,
            )
        )

        self.assertEqual(filename, "sample.ogg")
        self.assertEqual(content, ogg_bytes)
        self.assertEqual(content_type, "application/ogg")


if __name__ == "__main__":
    unittest.main()
