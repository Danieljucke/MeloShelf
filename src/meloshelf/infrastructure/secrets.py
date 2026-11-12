"""Secret storage in the operating system's credential store.

Windows: Credential Manager. macOS: Keychain. Linux: Secret Service, when available.
MeloShelf never writes a secret to its database, its files or its logs. When no secure
backend is available, saving is refused; the environment variable remains the fallback.
"""


class SecretStoreError(RuntimeError):
    pass


class MemorySecretStore:
    """Process-local storage for tests. Nothing is persisted."""
    backend = 'memory (this process only)'

    def __init__(self):
        self._values: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._values.pop(name, None)


class UnavailableSecretStore:
    """Used when no secure backend can be reached. Reads find nothing; writes are refused."""
    backend = 'none'

    def __init__(self, reason: str):
        self.reason = reason

    def available(self) -> bool:
        return False

    def get(self, name: str) -> str | None:
        return None

    def set(self, name: str, value: str) -> None:
        raise SecretStoreError(f'No secure credential store is available ({self.reason}).')

    def delete(self, name: str) -> None:
        return None


class KeyringSecretStore:
    """The operating system's credential store, through the ``keyring`` package."""

    def __init__(self, service: str = 'MeloShelf', keyring_module=None):
        self.service = service
        self._keyring = keyring_module
        self._backend_name = ''
        self._available = None

    def _module(self):
        if self._keyring is None:
            import keyring
            self._keyring = keyring
        return self._keyring

    def _probe(self) -> bool:
        if self._available is None:
            try:
                backend = self._module().get_keyring()
                self._backend_name = type(backend).__module__ + '.' + type(backend).__name__
                # The "fail" backend means no usable store exists on this system.
                self._available = 'fail' not in type(backend).__module__ and 'chainer' not in type(backend).__module__
            except Exception:  # any failure to query the backend means it is unusable
                self._available = False
                self._backend_name = 'unavailable'
        return self._available

    @property
    def backend(self) -> str:
        self._probe()
        return self._backend_name

    def available(self) -> bool:
        return self._probe()

    def get(self, name: str) -> str | None:
        if not self._probe():
            return None
        try:
            return self._module().get_password(self.service, name)
        except Exception as exc:
            raise SecretStoreError(f'The secure credential store could not be read ({type(exc).__name__}).') from exc

    def set(self, name: str, value: str) -> None:
        if not self._probe():
            raise SecretStoreError('No secure credential store is available on this computer.')
        try:
            self._module().set_password(self.service, name, value)
        except Exception as exc:
            raise SecretStoreError(f'The secure credential store refused to save ({type(exc).__name__}).') from exc

    def delete(self, name: str) -> None:
        if not self._probe():
            return None
        try:
            self._module().delete_password(self.service, name)
        except Exception:  # deleting an absent credential is not an error for the caller
            return None
