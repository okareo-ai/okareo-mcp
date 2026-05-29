"""Tests for the stateless signed-client_id format (FR-021/FR-022).

The signed-client_id is the core of stateless DCR: a self-describing,
HMAC-tamper-evident string that lets the OAuth Proxy recover a registration
record without any server-side state. These tests pin down:

- The wire format (prefix + payload + dot + mac)
- Round-trip integrity (encode → decode == original payload)
- All failure paths return ``InvalidSignedClientIdError``, not silent success
- HMAC verification is by value, NOT just bytewise (the constant-time check
  must use ``hmac.compare_digest``; we verify behavior, not implementation)
- Cross-key forgery is rejected
"""

from __future__ import annotations

import pytest

from src.auth.oauth_state import (
    InvalidSignedClientIdError,
    decode_signed_client_id,
    encode_signed_client_id,
)


SIGNING_KEY = "test-signing-key-very-secret-256-bits-of-entropy-plus-some"
OTHER_KEY = "different-signing-key-also-some-entropy-for-good-measure"


class TestRoundTrip:
    def test_encode_decode_recovers_payload(self):
        original = {
            "client_name": "VS Code",
            "redirect_uris": ["http://127.0.0.1:33418/"],
            "token_endpoint_auth_method": "none",
            "scope": "okareo:use",
            "iat": 1779000000,
        }
        encoded = encode_signed_client_id(original, SIGNING_KEY)
        decoded = decode_signed_client_id(encoded, SIGNING_KEY)
        assert decoded == original

    def test_wire_format_has_mcp_prefix(self):
        encoded = encode_signed_client_id({"x": 1}, SIGNING_KEY)
        assert encoded.startswith("mcp_")

    def test_wire_format_has_payload_dot_mac(self):
        encoded = encode_signed_client_id({"x": 1}, SIGNING_KEY)
        body = encoded[len("mcp_") :]
        assert "." in body
        payload_b64, mac_b64 = body.rsplit(".", 1)
        assert payload_b64  # non-empty
        assert mac_b64  # non-empty

    def test_payload_is_inspectable_by_anyone_holding_the_id(self):
        """The payload is base64'd JSON — not encrypted. Anyone with a
        client_id can read its contents (which they supplied anyway). This
        test pins down that property so a future change that adds secrets
        to the payload trips the test."""
        import base64
        import json

        original = {"redirect_uris": ["http://localhost/"], "scope": "x"}
        encoded = encode_signed_client_id(original, SIGNING_KEY)
        payload_b64 = encoded[len("mcp_") :].rsplit(".", 1)[0]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        decoded_payload = json.loads(base64.urlsafe_b64decode(padded))
        assert decoded_payload == original


class TestRejection:
    def test_wrong_signing_key_raises(self):
        encoded = encode_signed_client_id({"x": 1}, SIGNING_KEY)
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id(encoded, OTHER_KEY)

    def test_tampered_payload_raises(self):
        encoded = encode_signed_client_id({"redirect_uris": ["http://a/"]}, SIGNING_KEY)
        # Flip one byte in the payload segment.
        body = encoded[len("mcp_") :]
        payload_b64, mac_b64 = body.rsplit(".", 1)
        flipped = ("A" if payload_b64[0] != "A" else "B") + payload_b64[1:]
        tampered = f"mcp_{flipped}.{mac_b64}"
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id(tampered, SIGNING_KEY)

    def test_tampered_mac_raises(self):
        encoded = encode_signed_client_id({"x": 1}, SIGNING_KEY)
        body = encoded[len("mcp_") :]
        payload_b64, mac_b64 = body.rsplit(".", 1)
        flipped = ("A" if mac_b64[0] != "A" else "B") + mac_b64[1:]
        tampered = f"mcp_{payload_b64}.{flipped}"
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id(tampered, SIGNING_KEY)

    def test_missing_prefix_raises(self):
        encoded = encode_signed_client_id({"x": 1}, SIGNING_KEY)
        no_prefix = encoded[len("mcp_") :]
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id(no_prefix, SIGNING_KEY)

    def test_wrong_prefix_raises(self):
        encoded = encode_signed_client_id({"x": 1}, SIGNING_KEY)
        wrong = "xyz_" + encoded[len("mcp_") :]
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id(wrong, SIGNING_KEY)

    def test_missing_dot_raises(self):
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id("mcp_payloadonlyNoDot", SIGNING_KEY)

    def test_empty_payload_raises(self):
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id("mcp_.somemac", SIGNING_KEY)

    def test_empty_mac_raises(self):
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id("mcp_somepayload.", SIGNING_KEY)

    def test_non_string_client_id_raises(self):
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id(None, SIGNING_KEY)  # type: ignore[arg-type]
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id(12345, SIGNING_KEY)  # type: ignore[arg-type]

    def test_payload_not_valid_json_raises(self):
        # Hand-construct: prefix + non-JSON payload + valid-HMAC of that
        # garbage. The HMAC will verify but the decode will fail at json.loads.
        import base64
        import hashlib
        import hmac as _hmac

        garbage = b"not-valid-json{{{"
        payload_b64 = (
            base64.urlsafe_b64encode(garbage).rstrip(b"=").decode("ascii")
        )
        mac = _hmac.new(
            SIGNING_KEY.encode(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        mac_b64 = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
        crafted = f"mcp_{payload_b64}.{mac_b64}"
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id(crafted, SIGNING_KEY)

    def test_payload_not_dict_raises(self):
        # Same crafting trick but with a valid JSON value that's not an object
        import base64
        import hashlib
        import hmac as _hmac

        as_list = b"[1, 2, 3]"
        payload_b64 = (
            base64.urlsafe_b64encode(as_list).rstrip(b"=").decode("ascii")
        )
        mac = _hmac.new(
            SIGNING_KEY.encode(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        mac_b64 = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
        crafted = f"mcp_{payload_b64}.{mac_b64}"
        with pytest.raises(InvalidSignedClientIdError):
            decode_signed_client_id(crafted, SIGNING_KEY)


class TestStability:
    def test_encoding_is_deterministic_for_same_input(self):
        """Same payload + key → byte-identical client_id. JSON sort_keys
        guarantees this regardless of dict insertion order. Stability matters
        because it makes test fixtures + log inspection reproducible."""
        a = encode_signed_client_id(
            {"redirect_uris": ["http://x/"], "scope": "s"}, SIGNING_KEY
        )
        b = encode_signed_client_id(
            {"scope": "s", "redirect_uris": ["http://x/"]}, SIGNING_KEY
        )
        assert a == b

    def test_different_keys_produce_different_signatures(self):
        a = encode_signed_client_id({"x": 1}, SIGNING_KEY)
        b = encode_signed_client_id({"x": 1}, OTHER_KEY)
        # Payload segments are identical, MACs differ.
        a_body = a[len("mcp_") :]
        b_body = b[len("mcp_") :]
        a_payload, a_mac = a_body.rsplit(".", 1)
        b_payload, b_mac = b_body.rsplit(".", 1)
        assert a_payload == b_payload
        assert a_mac != b_mac
