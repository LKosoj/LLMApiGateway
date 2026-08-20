import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import llm_gateway_core.config.environment as environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOTENV_PATH = PROJECT_ROOT / ".env"
SETTINGS_MODULE_NAME = "llm_gateway_core.config.settings"


class SettingsEnvPriorityTests(unittest.TestCase):
    def test_deep_research_process_limits_are_strict_on_reload(self):
        settings_module = importlib.import_module(SETTINGS_MODULE_NAME)
        # A deployment may pin these keys in the project .env; the reload below must
        # still observe the code defaults, not whatever this checkout is running with.
        dotenv_patcher = patch.object(
            environment,
            "PROJECT_DOTENV",
            PROJECT_ROOT / ".env.absent",
        )
        dotenv_patcher.start()
        self.addCleanup(dotenv_patcher.stop)
        environment.load_application_environment.cache_clear()
        self.addCleanup(environment.load_application_environment.cache_clear)
        names = (
            "DEEP_RESEARCH_PROCESS_CAPACITY",
            "DEEP_RESEARCH_ADMISSION_TIMEOUT_SECONDS",
        )
        original_values = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ.pop(name, None)
            reloaded_module = importlib.reload(settings_module)
            self.assertEqual(reloaded_module.settings.deep_research_process_capacity, 1)
            self.assertEqual(
                reloaded_module.settings.deep_research_admission_timeout_seconds,
                5.0,
            )

            invalid_values = {
                names[0]: ("0", "-1", "invalid"),
                names[1]: ("0", "-1", "nan", "inf", "invalid"),
            }
            for name, values in invalid_values.items():
                for invalid in values:
                    with (
                        self.subTest(name=name, invalid=invalid),
                        patch.dict(os.environ, {name: invalid}, clear=False),
                        self.assertRaisesRegex(ValueError, name),
                    ):
                        importlib.reload(settings_module)

            with patch.dict(
                os.environ,
                {
                    names[0]: "3",
                    names[1]: "1.25",
                },
                clear=False,
            ):
                reloaded_module = importlib.reload(settings_module)
                self.assertEqual(
                    reloaded_module.settings.deep_research_process_capacity,
                    3,
                )
                self.assertEqual(
                    reloaded_module.settings.deep_research_admission_timeout_seconds,
                    1.25,
                )
        finally:
            for name, original_value in original_values.items():
                if original_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = original_value
            importlib.reload(settings_module)

    def test_upload_limits_are_strict_and_cross_validated_on_reload(self):
        settings_module = importlib.import_module(SETTINGS_MODULE_NAME)
        names = (
            "MAX_REQUEST_BODY_BYTES",
            "AUDIO_UPLOAD_MAX_BYTES",
            "IMAGE_UPLOAD_MAX_FILE_BYTES",
            "IMAGE_UPLOAD_MAX_TOTAL_BYTES",
            "PDF_UPLOAD_MAX_BYTES",
            "UPLOAD_INFLIGHT_MAX_BYTES",
            "UPLOAD_ADMISSION_TIMEOUT_SECONDS",
            "AUDIO_DECODED_MAX_BYTES",
        )
        original_values = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ.pop(name, None)
            reloaded_module = importlib.reload(settings_module)
            upload_settings = reloaded_module.settings
            self.assertEqual(upload_settings.audio_upload_max_bytes, 33_554_432)
            self.assertEqual(upload_settings.image_upload_max_file_bytes, 16_777_216)
            self.assertEqual(upload_settings.image_upload_max_total_bytes, 67_108_864)
            self.assertEqual(upload_settings.pdf_upload_max_bytes, 83_886_080)
            self.assertEqual(upload_settings.upload_inflight_max_bytes, 268_435_456)
            self.assertEqual(upload_settings.upload_admission_timeout_seconds, 5.0)
            self.assertEqual(upload_settings.audio_decoded_max_bytes, 100_663_296)

            for name in names[1:]:
                for invalid in ("0", "-1", "not-a-number"):
                    with (
                        self.subTest(name=name, invalid=invalid),
                        patch.dict(os.environ, {name: invalid}, clear=False),
                        self.assertRaisesRegex(ValueError, name),
                    ):
                        importlib.reload(settings_module)

            invalid_combinations = (
                {
                    "IMAGE_UPLOAD_MAX_FILE_BYTES": "9",
                    "IMAGE_UPLOAD_MAX_TOTAL_BYTES": "8",
                },
                {
                    "MAX_REQUEST_BODY_BYTES": "8",
                    "PDF_UPLOAD_MAX_BYTES": "9",
                },
                {
                    "AUDIO_UPLOAD_MAX_BYTES": "4",
                    "AUDIO_DECODED_MAX_BYTES": "4",
                    "IMAGE_UPLOAD_MAX_TOTAL_BYTES": "4",
                    "PDF_UPLOAD_MAX_BYTES": "4",
                    "UPLOAD_INFLIGHT_MAX_BYTES": "11",
                },
            )
            for values in invalid_combinations:
                with (
                    self.subTest(values=values),
                    patch.dict(os.environ, values, clear=False),
                    self.assertRaises(ValueError),
                ):
                    importlib.reload(settings_module)

            image_peak_values = {
                "MAX_REQUEST_BODY_BYTES": "8",
                "AUDIO_UPLOAD_MAX_BYTES": "1",
                "AUDIO_DECODED_MAX_BYTES": "1",
                "IMAGE_UPLOAD_MAX_FILE_BYTES": "4",
                "IMAGE_UPLOAD_MAX_TOTAL_BYTES": "8",
                "PDF_UPLOAD_MAX_BYTES": "1",
            }
            image_peak = settings_module._max_image_materialization_peak_bytes(
                max_file_bytes=4,
                max_total_bytes=8,
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        **image_peak_values,
                        "UPLOAD_INFLIGHT_MAX_BYTES": str(image_peak - 1),
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "UPLOAD_INFLIGHT_MAX_BYTES.*largest configured upload working set",
                ),
            ):
                importlib.reload(settings_module)

            with patch.dict(
                os.environ,
                {
                    **image_peak_values,
                    "UPLOAD_INFLIGHT_MAX_BYTES": str(image_peak),
                },
                clear=False,
            ):
                reloaded_module = importlib.reload(settings_module)
                self.assertEqual(
                    reloaded_module.settings.upload_inflight_max_bytes,
                    image_peak,
                )
        finally:
            for name, original_value in original_values.items():
                if original_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = original_value
            importlib.reload(settings_module)

    def test_json_response_limits_are_strict_and_cross_validated_on_reload(self):
        settings_module = importlib.import_module(SETTINGS_MODULE_NAME)
        names = (
            "JSON_RESPONSE_MAX_BYTES",
            "JSON_RESPONSE_INFLIGHT_MAX_BYTES",
        )
        original_values = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ.pop(name, None)
            reloaded_module = importlib.reload(settings_module)
            self.assertEqual(reloaded_module.settings.json_response_max_bytes, 8_388_608)
            self.assertEqual(
                reloaded_module.settings.json_response_inflight_max_bytes,
                33_554_432,
            )

            for name in names:
                for invalid in ("0", "-1", "not-a-number"):
                    with (
                        self.subTest(name=name, invalid=invalid),
                        patch.dict(os.environ, {name: invalid}, clear=False),
                        self.assertRaisesRegex(ValueError, name),
                    ):
                        importlib.reload(settings_module)

            with patch.dict(
                os.environ,
                {
                    "JSON_RESPONSE_MAX_BYTES": "512",
                    "JSON_RESPONSE_INFLIGHT_MAX_BYTES": "1024",
                },
                clear=False,
            ):
                reloaded_module = importlib.reload(settings_module)
                self.assertEqual(reloaded_module.settings.json_response_max_bytes, 512)
                self.assertEqual(
                    reloaded_module.settings.json_response_inflight_max_bytes,
                    1024,
                )

            with (
                patch.dict(
                    os.environ,
                    {
                        "JSON_RESPONSE_MAX_BYTES": "1025",
                        "JSON_RESPONSE_INFLIGHT_MAX_BYTES": "1024",
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "JSON_RESPONSE_MAX_BYTES.*JSON_RESPONSE_INFLIGHT_MAX_BYTES",
                ),
            ):
                importlib.reload(settings_module)
        finally:
            for name, original_value in original_values.items():
                if original_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = original_value
            importlib.reload(settings_module)

    def test_stream_observation_limits_are_validated_on_reload(self):
        settings_module = importlib.import_module(SETTINGS_MODULE_NAME)
        names = (
            "STREAM_OBSERVATION_BUFFER_MAX_BYTES",
            "STREAM_CHUNK_QUEUE_MAXSIZE",
            "STREAM_EVENT_MAX_BYTES",
        )
        original_values = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                for invalid in ("0", "-1", "not-a-number"):
                    with (
                        self.subTest(name=name, invalid=invalid),
                        patch.dict(os.environ, {name: invalid}, clear=False),
                        self.assertRaisesRegex(ValueError, name),
                    ):
                        importlib.reload(settings_module)

            with patch.dict(
                os.environ,
                {
                    "STREAM_OBSERVATION_BUFFER_MAX_BYTES": "1024",
                    "STREAM_CHUNK_QUEUE_MAXSIZE": "7",
                    "STREAM_EVENT_MAX_BYTES": "512",
                },
                clear=False,
            ):
                reloaded_module = importlib.reload(settings_module)
                self.assertEqual(
                    reloaded_module.settings.stream_observation_buffer_max_bytes,
                    1024,
                )
                self.assertEqual(reloaded_module.settings.stream_chunk_queue_maxsize, 7)
                self.assertEqual(reloaded_module.settings.stream_event_max_bytes, 512)

            with (
                patch.dict(
                    os.environ,
                    {
                        "STREAM_OBSERVATION_BUFFER_MAX_BYTES": "1024",
                        "STREAM_EVENT_MAX_BYTES": "1025",
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "STREAM_EVENT_MAX_BYTES.*STREAM_OBSERVATION_BUFFER_MAX_BYTES",
                ),
            ):
                importlib.reload(settings_module)
        finally:
            for name, original_value in original_values.items():
                if original_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = original_value
            importlib.reload(settings_module)

    def test_write_batcher_queue_size_env_is_validated_on_reload(self):
        settings_module = importlib.import_module(SETTINGS_MODULE_NAME)
        original_value = os.environ.get("WRITE_BATCHER_QUEUE_MAXSIZE")
        try:
            for invalid in ("0", "-1", "not-a-number"):
                with (
                    self.subTest(invalid=invalid),
                    patch.dict(
                        os.environ,
                        {"WRITE_BATCHER_QUEUE_MAXSIZE": invalid},
                        clear=False,
                    ),
                    self.assertRaisesRegex(ValueError, "WRITE_BATCHER_QUEUE_MAXSIZE"),
                ):
                    importlib.reload(settings_module)

            with patch.dict(
                os.environ,
                {"WRITE_BATCHER_QUEUE_MAXSIZE": "17"},
                clear=False,
            ):
                reloaded_module = importlib.reload(settings_module)
                self.assertEqual(reloaded_module.settings.write_batcher_queue_maxsize, 17)
        finally:
            if original_value is None:
                os.environ.pop("WRITE_BATCHER_QUEUE_MAXSIZE", None)
            else:
                os.environ["WRITE_BATCHER_QUEUE_MAXSIZE"] = original_value
            importlib.reload(settings_module)

    def test_process_env_overrides_dotenv_values_after_settings_reload(self):
        original_exists = DOTENV_PATH.exists()
        original_content = DOTENV_PATH.read_text(encoding="utf-8") if original_exists else None
        settings_module = importlib.import_module(SETTINGS_MODULE_NAME)

        try:
            DOTENV_PATH.write_text("GATEWAY_API_KEY=dotenv-value\n", encoding="utf-8")

            with patch.dict(os.environ, {"GATEWAY_API_KEY": "process-value"}, clear=False):
                reloaded_module = importlib.reload(settings_module)
                self.assertEqual(reloaded_module.settings.gateway_api_key, "process-value")
        finally:
            if original_exists and original_content is not None:
                DOTENV_PATH.write_text(original_content, encoding="utf-8")
            elif DOTENV_PATH.exists():
                DOTENV_PATH.unlink()

            importlib.reload(settings_module)


if __name__ == "__main__":
    unittest.main()
