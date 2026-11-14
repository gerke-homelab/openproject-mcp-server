"""Unit tests for the TLS trust configuration of OpenProjectClient."""

import ssl

import certifi
import pytest
import truststore

from src.client import OpenProjectClient


@pytest.fixture(autouse=True)
def _no_env_bundle(monkeypatch):
    monkeypatch.delenv("OPENPROJECT_CA_BUNDLE", raising=False)


@pytest.fixture
def loaded_cafiles(monkeypatch):
    """CA files loaded into a truststore context; the real load still runs.

    truststore.SSLContext.get_ca_certs() raises NotImplementedError, so the
    loaded CAs cannot be read back from the context.
    """
    calls = []
    original = truststore.SSLContext.load_verify_locations

    def record(self, cafile=None, capath=None, cadata=None):
        calls.append(cafile)
        return original(self, cafile=cafile, capath=capath, cadata=cadata)

    monkeypatch.setattr(truststore.SSLContext, "load_verify_locations", record)
    return calls


def _client(**kwargs):
    return OpenProjectClient(base_url="https://op.example.com", api_key="k", **kwargs)


def test_default_context_trusts_the_os_store():
    ctx = _client().ssl_context

    assert isinstance(ctx, truststore.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_default_context_loads_no_extra_ca(loaded_cafiles):
    _client()

    assert loaded_cafiles == []


def test_env_bundle_adds_its_cas(monkeypatch, loaded_cafiles):
    monkeypatch.setenv("OPENPROJECT_CA_BUNDLE", certifi.where())
    client = _client()

    assert isinstance(client.ssl_context, truststore.SSLContext)
    assert loaded_cafiles == [certifi.where()]


def test_explicit_bundle_wins_over_env(monkeypatch, tmp_path, loaded_cafiles):
    monkeypatch.setenv("OPENPROJECT_CA_BUNDLE", str(tmp_path / "absent.pem"))

    _client(ca_bundle=certifi.where())

    assert loaded_cafiles == [certifi.where()]


def test_missing_bundle_file_fails_with_its_path(monkeypatch, tmp_path):
    missing = tmp_path / "absent.pem"
    monkeypatch.setenv("OPENPROJECT_CA_BUNDLE", str(missing))

    with pytest.raises(ValueError, match="OPENPROJECT_CA_BUNDLE") as exc:
        _client()

    assert str(missing) in str(exc.value)
