"""Secret storage (API keys, printer access codes) for Filamatch.

When the OS keychain works, secrets never go into ``settings.json``:

* macOS: the login keychain, through the Security framework (``ctypes``,
  generic-password items under the service :data:`SERVICE`).  Items are
  created by the Fusion process, so Fusion reads them back without a prompt.
* Windows: the Windows Credential Manager, through ``advapi32`` (``ctypes``,
  generic credentials named ``Filamatch:<account>``, persisted for the local
  machine and current user).
* other systems, or any keychain failure: :func:`set_secret` returns
  ``False`` and the caller keeps the old plain settings file behaviour.

``FILAMATCH_SECRET_BACKEND`` selects ``keychain`` (default: the OS store of
this platform), ``memory`` (tests; lost when the process ends) or ``none``.

Secret values are never logged.  This module imports cleanly outside Fusion
360 (standard library only); its self-test uses the memory backend and never
touches the real keychain.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import traceback
from typing import Dict, Optional, Tuple

logger = logging.getLogger("Filamatch." + __name__.rsplit(".", 1)[-1])

#: Keychain service name shared by every Filamatch secret.
SERVICE = "Filamatch"

#: Service names of earlier versions (FusionFilament Bridge). A secret found
#: there moves to :data:`SERVICE` on its first read.
LEGACY_SERVICES = ("FusionFilamentBridge",)

#: Environment variable that selects the backend.
BACKEND_ENV = "FILAMATCH_SECRET_BACKEND"

_ERR_SEC_ITEM_NOT_FOUND = -25300

# Windows Credential Manager constants (wincred.h / winerror.h).
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
#: CredWriteW refuses blobs larger than CRED_MAX_CREDENTIAL_BLOB_SIZE.
_CRED_MAX_BLOB_SIZE = 5 * 512


class SecretStoreError(Exception):
    """The secret backend failed."""


class MemoryBackend:
    """In-process secret store for tests."""

    name = "memory"

    def __init__(self) -> None:
        self._values: Dict[Tuple[str, str], str] = {}

    def get(self, account: str, service: str = SERVICE) -> Optional[str]:
        return self._values.get((service, account))

    def set(self, account: str, value: str) -> None:
        self._values[(SERVICE, account)] = value

    def delete(self, account: str, service: str = SERVICE) -> bool:
        return self._values.pop((service, account), None) is not None


class MacKeychainBackend:
    """Generic-password items in the macOS login keychain."""

    name = "keychain"

    def __init__(self) -> None:
        from ctypes import POINTER, c_char_p, c_int32, c_uint32, c_void_p

        sec = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        sec.SecKeychainFindGenericPassword.argtypes = [
            c_void_p, c_uint32, c_char_p, c_uint32, c_char_p,
            POINTER(c_uint32), POINTER(c_void_p), POINTER(c_void_p),
        ]
        sec.SecKeychainFindGenericPassword.restype = c_int32
        sec.SecKeychainAddGenericPassword.argtypes = [
            c_void_p, c_uint32, c_char_p, c_uint32, c_char_p,
            c_uint32, c_char_p, POINTER(c_void_p),
        ]
        sec.SecKeychainAddGenericPassword.restype = c_int32
        sec.SecKeychainItemModifyAttributesAndData.argtypes = [c_void_p, c_void_p, c_uint32, c_char_p]
        sec.SecKeychainItemModifyAttributesAndData.restype = c_int32
        sec.SecKeychainItemDelete.argtypes = [c_void_p]
        sec.SecKeychainItemDelete.restype = c_int32
        sec.SecKeychainItemFreeContent.argtypes = [c_void_p, c_void_p]
        sec.SecKeychainItemFreeContent.restype = c_int32
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFRelease.restype = None
        self._sec = sec
        self._cf = cf
        self._service = SERVICE.encode("utf-8")

    def _find_item(self, account: bytes, service: Optional[bytes] = None) -> Optional[ctypes.c_void_p]:
        """Return the keychain item for ``account`` (caller releases it)."""
        service = service or self._service
        item = ctypes.c_void_p()
        status = self._sec.SecKeychainFindGenericPassword(
            None, len(service), service, len(account), account,
            None, None, ctypes.byref(item),
        )
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return None
        if status != 0:
            raise SecretStoreError("Keychain lookup failed (OSStatus {}).".format(status))
        return item

    def get(self, account: str, service: str = SERVICE) -> Optional[str]:
        name = account.encode("utf-8")
        service_name = service.encode("utf-8")
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        status = self._sec.SecKeychainFindGenericPassword(
            None, len(service_name), service_name, len(name), name,
            ctypes.byref(length), ctypes.byref(data), None,
        )
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return None
        if status != 0:
            raise SecretStoreError("Keychain read failed (OSStatus {}).".format(status))
        try:
            return ctypes.string_at(data, length.value).decode("utf-8")
        finally:
            self._sec.SecKeychainItemFreeContent(None, data)

    def set(self, account: str, value: str) -> None:
        name = account.encode("utf-8")
        secret = value.encode("utf-8")
        item = self._find_item(name)
        if item is not None:
            try:
                status = self._sec.SecKeychainItemModifyAttributesAndData(
                    item, None, len(secret), secret
                )
            finally:
                self._cf.CFRelease(item)
        else:
            status = self._sec.SecKeychainAddGenericPassword(
                None, len(self._service), self._service, len(name), name,
                len(secret), secret, None,
            )
        if status != 0:
            raise SecretStoreError("Keychain write failed (OSStatus {}).".format(status))

    def delete(self, account: str, service: str = SERVICE) -> bool:
        item = self._find_item(account.encode("utf-8"), service.encode("utf-8"))
        if item is None:
            return False
        try:
            status = self._sec.SecKeychainItemDelete(item)
        finally:
            self._cf.CFRelease(item)
        if status != 0:
            raise SecretStoreError("Keychain delete failed (OSStatus {}).".format(status))
        return True


class WindowsCredentialBackend:
    """Generic credentials in the Windows Credential Manager.

    The target name is ``<service>:<account>`` (``Filamatch:bambuddy_api_key``).
    The secret is stored as UTF-8 bytes.  Only constructed on Windows.
    """

    name = "credential manager"

    def __init__(self) -> None:
        if not sys.platform.startswith("win"):
            raise SecretStoreError("The Windows Credential Manager exists only on Windows.")
        from ctypes import POINTER, Structure, c_ubyte, c_void_p, wintypes

        class FILETIME(Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIALW(Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", POINTER(c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, POINTER(POINTER(CREDENTIALW)),
        ]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredWriteW.argtypes = [POINTER(CREDENTIALW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        advapi32.CredDeleteW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [c_void_p]
        advapi32.CredFree.restype = None
        self._advapi32 = advapi32
        self._credential_type = CREDENTIALW
        self._c_ubyte = c_ubyte
        self._pointer = POINTER

    @staticmethod
    def _target(account: str, service: str) -> str:
        return "{}:{}".format(service, account)

    def get(self, account: str, service: str = SERVICE) -> Optional[str]:
        credential = self._pointer(self._credential_type)()
        ok = self._advapi32.CredReadW(
            self._target(account, service), _CRED_TYPE_GENERIC, 0, ctypes.byref(credential)
        )
        if not ok:
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            if error == _ERROR_NOT_FOUND:
                return None
            raise SecretStoreError("Credential Manager read failed (error {}).".format(error))
        try:
            size = int(credential.contents.CredentialBlobSize)
            if size <= 0 or not credential.contents.CredentialBlob:
                return ""
            return ctypes.string_at(credential.contents.CredentialBlob, size).decode("utf-8")
        finally:
            self._advapi32.CredFree(credential)

    def set(self, account: str, value: str) -> None:
        secret = value.encode("utf-8")
        if len(secret) > _CRED_MAX_BLOB_SIZE:
            raise SecretStoreError("Secret is too large for the Credential Manager.")
        blob = (self._c_ubyte * max(1, len(secret))).from_buffer_copy(secret or b"\0")
        credential = self._credential_type()
        credential.Flags = 0
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = self._target(account, SERVICE)
        credential.CredentialBlobSize = len(secret)
        credential.CredentialBlob = ctypes.cast(blob, self._pointer(self._c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.AttributeCount = 0
        credential.Attributes = None
        credential.UserName = account
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise SecretStoreError("Credential Manager write failed (error {}).".format(error))

    def delete(self, account: str, service: str = SERVICE) -> bool:
        if self._advapi32.CredDeleteW(self._target(account, service), _CRED_TYPE_GENERIC, 0):
            return True
        error = ctypes.get_last_error()  # type: ignore[attr-defined]
        if error == _ERROR_NOT_FOUND:
            return False
        raise SecretStoreError("Credential Manager delete failed (error {}).".format(error))


_BACKEND: Optional[object] = None
_BACKEND_LOADED = False


def backend() -> Optional[object]:
    """Return the active backend, or ``None`` when secrets stay in settings."""
    global _BACKEND, _BACKEND_LOADED
    if _BACKEND_LOADED:
        return _BACKEND
    _BACKEND_LOADED = True
    choice = os.environ.get(BACKEND_ENV, "").strip().lower() or "keychain"
    if choice == "memory":
        _BACKEND = MemoryBackend()
    elif choice == "keychain" and sys.platform == "darwin":
        try:
            _BACKEND = MacKeychainBackend()
        except Exception:
            logger.warning("macOS keychain is unavailable; secrets stay in settings.", exc_info=True)
            _BACKEND = None
    elif choice == "keychain" and sys.platform.startswith("win"):
        try:
            _BACKEND = WindowsCredentialBackend()
        except Exception:
            logger.warning(
                "Windows Credential Manager is unavailable; secrets stay in settings.",
                exc_info=True,
            )
            _BACKEND = None
    else:
        _BACKEND = None
    return _BACKEND


def reset_backend() -> None:
    """Forget the cached backend (tests change the environment)."""
    global _BACKEND, _BACKEND_LOADED
    _BACKEND = None
    _BACKEND_LOADED = False


def is_available() -> bool:
    """Return whether secrets can be kept out of the settings file."""
    return backend() is not None


def get_secret(account: str) -> Optional[str]:
    """Return the stored secret, or ``None`` when absent or unavailable."""
    store = backend()
    if store is None:
        return None
    try:
        value = store.get(account)  # type: ignore[attr-defined]
        if value is None:
            value = _move_legacy_secret(store, account)
        return value
    except Exception as exc:
        logger.warning("Could not read secret %r from the %s: %s", account, store.name, exc)  # type: ignore[attr-defined]
        return None


def _move_legacy_secret(store: object, account: str) -> Optional[str]:
    """Move a secret saved under a legacy service name to :data:`SERVICE`.

    The legacy value is returned even when the move fails (for example a
    locked keychain); the move is tried again on the next read.
    """
    for legacy in LEGACY_SERVICES:
        value = store.get(account, service=legacy)  # type: ignore[attr-defined]
        if value is None:
            continue
        try:
            store.set(account, value)  # type: ignore[attr-defined]
            store.delete(account, service=legacy)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning(
                "Could not move secret %r from the old %r entry; using it in place: %s",
                account, legacy, exc,
            )
        else:
            logger.info("Moved secret %r from the old %r keychain entry.", account, legacy)
        return value
    return None


def set_secret(account: str, value: str) -> bool:
    """Store a secret; return ``False`` so the caller can fall back."""
    store = backend()
    if store is None:
        return False
    try:
        store.set(account, value)  # type: ignore[attr-defined]
        return True
    except Exception as exc:
        logger.warning("Could not store secret %r in the %s: %s", account, store.name, exc)  # type: ignore[attr-defined]
        return False


def delete_secret(account: str) -> bool:
    """Remove a stored secret; return whether one was removed."""
    store = backend()
    if store is None:
        return False
    try:
        return bool(store.delete(account))  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("Could not delete secret %r from the %s: %s", account, store.name, exc)  # type: ignore[attr-defined]
        return False


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _with_backend(choice: str) -> None:
    os.environ[BACKEND_ENV] = choice
    reset_backend()


def _check_memory_backend() -> None:
    """Set, read, replace and delete through the memory backend."""
    _with_backend("memory")
    _expect(is_available(), "memory backend is available")
    _expect(get_secret("bambuddy_api_key") is None, "absent secret reads as None")
    _expect(set_secret("bambuddy_api_key", "bb_one"), "secret is stored")
    _expect(set_secret("bambuddy_api_key", "bb_two"), "secret is replaced")
    _expect(get_secret("bambuddy_api_key") == "bb_two", "latest secret is returned")
    _expect(delete_secret("bambuddy_api_key"), "secret is deleted")
    _expect(not delete_secret("bambuddy_api_key"), "second delete finds nothing")


def _check_legacy_service_move() -> None:
    """A secret saved by FusionFilament Bridge moves to the new service name."""
    _with_backend("memory")
    store = backend()
    store._values[(LEGACY_SERVICES[0], "bambuddy_api_key")] = "bb_old"  # type: ignore[attr-defined]
    _expect(get_secret("bambuddy_api_key") == "bb_old", "legacy secret is returned")
    _expect(store.get("bambuddy_api_key") == "bb_old", "secret now under the new service")  # type: ignore[attr-defined]
    _expect(
        store.get("bambuddy_api_key", service=LEGACY_SERVICES[0]) is None,  # type: ignore[attr-defined]
        "legacy entry is removed",
    )


def _check_legacy_move_failure() -> None:
    """A legacy secret is still returned when moving it fails."""
    _with_backend("memory")
    store = backend()
    store._values[(LEGACY_SERVICES[0], "bambuddy_api_key")] = "bb_old"  # type: ignore[attr-defined]

    def refuse(account: str, value: str) -> None:
        raise SecretStoreError("simulated locked keychain")

    store.set = refuse  # type: ignore[attr-defined,assignment]
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        _expect(get_secret("bambuddy_api_key") == "bb_old", "legacy value survives a failed move")
    finally:
        logger.setLevel(previous)
    _expect(
        store.get("bambuddy_api_key", service=LEGACY_SERVICES[0]) == "bb_old",  # type: ignore[attr-defined]
        "legacy entry is kept when the move fails",
    )


def _check_windows_backend_guard() -> None:
    """The Windows backend is only built on Windows."""
    if sys.platform.startswith("win"):
        return
    _with_backend("keychain")
    _expect(
        not isinstance(backend(), WindowsCredentialBackend),
        "the Windows backend is not selected on this platform",
    )
    try:
        WindowsCredentialBackend()
    except SecretStoreError:
        pass
    else:
        raise AssertionError("WindowsCredentialBackend must refuse to start off Windows")
    _expect(
        WindowsCredentialBackend._target("bambuddy_api_key", SERVICE) == "Filamatch:bambuddy_api_key",
        "target name is service:account",
    )


def _check_disabled_backend() -> None:
    """With no backend, callers are told to fall back."""
    _with_backend("none")
    _expect(not is_available(), "none backend is unavailable")
    _expect(not set_secret("bambuddy_api_key", "bb_x"), "set reports the fallback")
    _expect(get_secret("bambuddy_api_key") is None, "get returns None")


def _selftest() -> int:
    """Run the secret store self-test (never touches the real keychain)."""
    previous = os.environ.get(BACKEND_ENV)
    scenarios = (
        ("memory backend stores, replaces and deletes secrets", _check_memory_backend),
        ("a secret under the old service name moves to the new one", _check_legacy_service_move),
        ("a legacy secret is returned when moving it fails", _check_legacy_move_failure),
        ("the Windows backend is only built on Windows", _check_windows_backend_guard),
        ("a disabled backend makes callers fall back", _check_disabled_backend),
    )
    failures = 0
    try:
        for label, check in scenarios:
            try:
                check()
            except Exception:
                failures += 1
                print("FAIL:", label)
                traceback.print_exc()
            else:
                print("PASS:", label)
    finally:
        if previous is None:
            os.environ.pop(BACKEND_ENV, None)
        else:
            os.environ[BACKEND_ENV] = previous
        reset_backend()
    if failures:
        print("{} secret store self-test(s) FAILED".format(failures))
        return 1
    print("PASS: all secret store self-tests completed")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
