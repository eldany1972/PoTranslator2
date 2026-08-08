from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


class CredentialError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialError("Encrypted credential storage is only available on Windows.")
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "PoTranslator API key",
        None,
        None,
        None,
        0,
        ctypes.byref(output),
    ):
        raise CredentialError("Windows could not encrypt the API key.")
    # Keep the ctypes buffer alive until CryptProtectData returns.
    _ = source_buffer
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialError("Encrypted credential storage is only available on Windows.")
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise CredentialError("Windows could not decrypt the API key for this user.")
    _ = source_buffer
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


PROVIDERS = ("openai", "anthropic")

# Environment variables checked before the encrypted store, per provider.
# Anthropic's official SDKs read ANTHROPIC_API_KEY; CLAUDE_API_KEY is accepted
# too since that's what people commonly expect to set by name.
_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
}


def credential_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return Path.home() / ".potranslator" / "credentials.json"
    return Path(base) / "Ultraton" / "PoTranslator" / "credentials.json"


def _read_store() -> dict[str, dict[str, str]]:
    """Return {provider: {"dpapi": "..."}}, migrating the old single-key format."""
    source = credential_path()
    if not source.exists():
        return {}
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if raw.get("version") == 1 and "dpapi" in raw:
        # Legacy format: a single OpenAI key stored at the top level.
        return {raw.get("provider", "openai"): {"dpapi": raw["dpapi"]}}
    return dict(raw.get("keys", {}))


def _write_store(keys: dict[str, dict[str, str]]) -> None:
    destination = credential_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"version": 2, "keys": keys}), encoding="utf-8")


def save_api_key(provider: str, api_key: str) -> None:
    key = api_key.strip()
    if not key:
        raise CredentialError("The API key cannot be empty.")
    encrypted = _protect(key.encode("utf-8"))
    keys = _read_store()
    keys[provider] = {"dpapi": base64.b64encode(encrypted).decode("ascii")}
    _write_store(keys)


def load_api_key(provider: str) -> str:
    for env_name in _ENV_VARS.get(provider, ()):
        environment_key = os.environ.get(env_name, "").strip()
        if environment_key:
            return environment_key
    keys = _read_store()
    entry = keys.get(provider)
    if not entry:
        return ""
    try:
        return _unprotect(base64.b64decode(entry["dpapi"])).decode("utf-8")
    except (KeyError, ValueError, CredentialError):
        return ""


def delete_api_key(provider: str) -> None:
    keys = _read_store()
    if provider in keys:
        del keys[provider]
        _write_store(keys)
