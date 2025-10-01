"""Helpers for persisting integration test secrets locally."""

from __future__ import annotations

import getpass
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

_DEFAULT_SECRET_PATH = Path.home() / ".petsafe" / "integration_secrets.json"


@dataclass
class SecretStore:
    """Persist PetSafe integration secrets between test runs.

    The store keeps the PetSafe email address, the most recent verification code,
    and the current set of OAuth tokens. Secrets are written to a file that is
    only readable by the current user so that they are not checked in to the
    repository.
    """

    path: Path = _DEFAULT_SECRET_PATH
    data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                try:
                    self.data.update(json.load(handle))
                except json.JSONDecodeError:
                    # If the file is corrupted we fall back to an empty store.
                    self.data = {}
        self._ensure_permissions()

    def _ensure_permissions(self) -> None:
        """Ensure the secret file is only readable/writable by the user."""

        if not self.path.exists():
            return
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except PermissionError:
            # On platforms where chmod is not available (e.g. Windows) we ignore
            # the error and rely on the default permissions.
            pass

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
        self._ensure_permissions()

    def get(self, key: str, default: Any | None = None) -> Any | None:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def prompt(self, key: str, prompt: str, *, secret: bool = False) -> str:
        """Prompt the user for a secret value and persist it."""

        existing = self.data.get(key)
        if existing:
            return existing
        if secret:
            value = getpass.getpass(prompt)
        else:
            value = input(prompt)
        value = value.strip()
        if not value:
            raise ValueError(f"A value for '{key}' is required")
        self.set(key, value)
        return value

    def update(self, values: Dict[str, Any]) -> None:
        self.data.update(values)
        self.save()
