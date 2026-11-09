"""The committed CIMD document is Aurelius's permanent OAuth identity. Guard it."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from app.integrations.questrade.auth import cimd_document

DOC = Path(__file__).resolve().parents[3] / "deploy" / "static" / "oauth" / "client-metadata.json"
CLIENT_ID = "https://aurelius.rivenpath.ai/oauth/client-metadata.json"


def test_client_id_is_the_permanent_url():
    doc = json.loads(DOC.read_text(encoding="utf-8"))
    assert doc["client_id"] == CLIENT_ID


def test_document_matches_generator_and_is_a_public_client():
    doc = json.loads(DOC.read_text(encoding="utf-8"))
    assert doc == cimd_document(CLIENT_ID, doc["redirect_uris"])
    assert doc["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in doc
    assert doc["grant_types"] == ["authorization_code", "refresh_token"]


def test_redirect_uris_are_our_host_or_loopback_only():
    doc = json.loads(DOC.read_text(encoding="utf-8"))
    for uri in doc["redirect_uris"]:
        u = urlsplit(uri)
        assert (u.scheme, u.hostname) in {("https", "aurelius.rivenpath.ai"), ("http", "127.0.0.1")}, uri
