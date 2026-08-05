"""Shared provider endpoint resolution and non-secret identity helpers."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit


DEFAULT_PROVIDER_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
PROVIDER_ENDPOINT_ENV = {
    "openai": "OPENAI_BASE_URL",
    "anthropic": "ANTHROPIC_BASE_URL",
}
_ENDPOINT_KEYS = ("base_url", "endpoint", "api_base")
_SENSITIVE_URL_PARTS = "userinfo, query, or fragment"


def _configured_endpoint(provider: str, llm_config: Mapping[str, object] | None,
                         environ: Mapping[str, str]) -> str:
    config = llm_config if isinstance(llm_config, Mapping) else {}
    for key in _ENDPOINT_KEYS:
        value = config.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"LLM endpoint config {key!r} must be a string")
        if isinstance(value, str) and value.strip():
            return value.strip()
    env_name = PROVIDER_ENDPOINT_ENV.get(provider)
    if env_name:
        value = environ.get(env_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        return DEFAULT_PROVIDER_ENDPOINTS[provider]
    except KeyError as exc:
        raise ValueError(f"unknown LLM provider {provider!r}") from exc


def _parts(value: str):
    parsed = urlsplit(str(value).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("LLM endpoint must be an absolute HTTP(S) URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("LLM endpoint has an invalid port") from exc
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    # Keep the raw userinfo spelling for the effective URL. It is never
    # returned by the safe display helper or persisted in a manifest.
    userinfo = ""
    if "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@"
    netloc = userinfo + host + (f":{port}" if port is not None else "")
    path = parsed.path.rstrip("/")
    return parsed, netloc, path


def normalize_provider_endpoint(value: str, *, allow_sensitive: bool = True) -> str:
    """Normalize an effective URL, optionally rejecting secret-bearing parts.

    Runtime normalization preserves userinfo and query because they can be
    routing-relevant. Config-file values use ``allow_sensitive=False`` so no
    secret-bearing URL can enter persisted experiment provenance.
    """
    parsed, netloc, path = _parts(value)
    if not allow_sensitive and (parsed.username is not None
                                or parsed.password is not None
                                or parsed.query or parsed.fragment):
        raise ValueError(f"LLM endpoint cannot contain {_SENSITIVE_URL_PARTS}")
    # Fragments are never sent as HTTP routing data, so they are dropped from
    # the effective URL even for environment-selected endpoints.
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def resolve_provider_endpoint(
        provider: str, llm_config: Mapping[str, object] | None = None,
        *, environ: Mapping[str, str] | None = None) -> str:
    """Resolve the exact effective endpoint passed to the provider SDK."""
    provider_name = str(provider or "").strip().lower()
    if provider_name not in DEFAULT_PROVIDER_ENDPOINTS:
        raise ValueError(f"unknown LLM provider {provider!r}")
    return normalize_provider_endpoint(_configured_endpoint(
        provider_name, llm_config, os.environ if environ is None else environ))


def safe_provider_endpoint(value: str) -> str:
    """Return a sanitized display identity for an already-resolved URL."""
    parsed, _netloc, path = _parts(value)
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    netloc = host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def hash_provider_endpoint(value: str) -> str:
    """Hash an already-resolved effective URL."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def provider_endpoint_identity(
        provider: str, llm_config: Mapping[str, object] | None = None,
        *, environ: Mapping[str, str] | None = None) -> str:
    """Return a safe endpoint display identity without userinfo/query/fragment."""
    return safe_provider_endpoint(resolve_provider_endpoint(
        provider, llm_config, environ=environ))


def provider_endpoint_hash(
        provider: str, llm_config: Mapping[str, object] | None = None,
        *, environ: Mapping[str, str] | None = None) -> str:
    """Hash the exact effective URL without persisting the URL itself."""
    return hash_provider_endpoint(resolve_provider_endpoint(
        provider, llm_config, environ=environ))


def client_endpoint_kwargs(
        provider: str, llm_config: Mapping[str, object] | None = None) -> dict:
    """Pass the resolved URL explicitly, including provider defaults."""
    return {"base_url": resolve_provider_endpoint(provider, llm_config)}
