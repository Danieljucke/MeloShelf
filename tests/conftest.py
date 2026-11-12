from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import pytest


@pytest.fixture(autouse=True)
def isolated_secret_store(monkeypatch):
    """Tests never touch the operating system's credential store."""
    from meloshelf.infrastructure.secrets import MemorySecretStore
    monkeypatch.setattr('meloshelf.application.webapp.KeyringSecretStore', MemorySecretStore)
