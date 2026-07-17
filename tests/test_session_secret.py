import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.middleware import auth as auth_module
from llm_gateway_core.services import session_secret

GATEWAY_API_KEY_TARGET = "llm_gateway_core.middleware.auth.settings.gateway_api_key"


class SessionSecretStorageTests(unittest.TestCase):
    """Issuing, reuse, rotation and refusal of the stored secret itself."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / session_secret.SECRET_FILENAME

    def test_first_start_issues_an_owner_only_secret(self):
        secret = session_secret.load_or_create_session_secret(self.path, "master-key")

        self.assertEqual(len(secret), 32)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_secret_is_reused_while_the_api_key_is_unchanged(self):
        first = session_secret.load_or_create_session_secret(self.path, "master-key")
        second = session_secret.load_or_create_session_secret(self.path, "master-key")

        self.assertEqual(first, second)

    def test_rotating_the_api_key_replaces_the_secret(self):
        first = session_secret.load_or_create_session_secret(self.path, "master-key")
        second = session_secret.load_or_create_session_secret(self.path, "rotated-key")

        self.assertNotEqual(first, second)
        # The replacement is durable: the rotated key owns the file from now on.
        self.assertEqual(
            second,
            session_secret.load_or_create_session_secret(self.path, "rotated-key"),
        )

    def test_corrupt_secret_is_refused_instead_of_silently_reissued(self):
        # Reissuing would log every browser out without saying why.
        self.path.write_text("{ not json", encoding="utf-8")

        with self.assertRaises(session_secret.SessionSecretError):
            session_secret.load_or_create_session_secret(self.path, "master-key")

    def test_unsupported_version_is_refused(self):
        session_secret.load_or_create_session_secret(self.path, "master-key")
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["version"] = session_secret.SECRET_VERSION + 1
        self.path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaises(session_secret.SessionSecretError):
            session_secret.load_or_create_session_secret(self.path, "master-key")

    def test_no_api_key_leaves_session_auth_off_without_touching_the_state_dir(self):
        self.assertIsNone(session_secret.get_session_secret(""))
        self.assertIsNone(session_secret.get_session_secret("   "))
        self.assertIsNone(session_secret.get_session_secret(None))


class SessionCookieTests(unittest.TestCase):
    """What the separate secret means for cookies the gateway really issues."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state_dir = Path(directory.name)
        # A private state directory, so these tests never reissue the secret of
        # a gateway deployed from this checkout.
        patcher = patch.object(session_secret, "resolve_db_dir", lambda: state_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._restart_gateway()
        self.addCleanup(self._restart_gateway)

    def _restart_gateway(self):
        """Drop the cached secret, which is all a restart does to it."""
        session_secret._cached_secret = None

    def test_cookie_survives_a_restart_when_the_api_key_is_unchanged(self):
        with patch(GATEWAY_API_KEY_TARGET, "master-key"):
            session_id = auth_module.create_authenticated_session(role=auth_module.ROLE_MASTER)
            self.assertTrue(auth_module._has_valid_session(session_id))

            self._restart_gateway()

            self.assertTrue(auth_module._has_valid_session(session_id))

    def test_rotating_the_api_key_invalidates_existing_cookies(self):
        # The README promises a cookie lives until logout or a GATEWAY_API_KEY
        # change. Signing with a separate secret must not cost that guarantee,
        # because logout is a no-op and rotation is the only way to end sessions.
        with patch(GATEWAY_API_KEY_TARGET, "master-key"):
            session_id = auth_module.create_authenticated_session(role=auth_module.ROLE_MASTER)

        self._restart_gateway()

        with patch(GATEWAY_API_KEY_TARGET, "rotated-key"):
            self.assertFalse(auth_module._has_valid_session(session_id))

    def test_cookie_signature_does_not_reveal_the_gateway_api_key(self):
        # The reason this module exists: a cookie holder knows both the payload
        # and its signature, so a signature keyed by GATEWAY_API_KEY would let
        # them grind candidate master keys offline, out of IpBlockGuard's reach.
        api_key = "master-key"
        with patch(GATEWAY_API_KEY_TARGET, api_key):
            session_id = auth_module.create_authenticated_session(role=auth_module.ROLE_MASTER)

        issued_at, expires_at, nonce, role, _key_id, signature = session_id.split(".")
        payload = auth_module._build_session_signature_payload(
            int(issued_at),
            int(expires_at),
            nonce,
            role,
            None,
        )
        signed_with_the_master_key = hmac.new(
            api_key.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()

        self.assertNotEqual(signature, signed_with_the_master_key)


if __name__ == "__main__":
    unittest.main()
