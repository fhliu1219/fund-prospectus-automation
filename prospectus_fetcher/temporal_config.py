"""Temporal connection settings shared by local and Cloud clients."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from temporalio.client import Client, TLSConfig

from .temporal_contracts import TEMPORAL_TASK_QUEUE


def _optional(value: Optional[str]) -> Optional[str]:
    stripped = value.strip() if value else ""
    return stripped or None


def _boolean(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected a boolean value, received {value!r}")


@dataclass(frozen=True)
class TemporalConnectionSettings:
    address: str = "localhost:7233"
    namespace: str = "default"
    task_queue: str = TEMPORAL_TASK_QUEUE
    tls_enabled: bool = False
    api_key: Optional[str] = field(default=None, repr=False)
    client_cert_path: Optional[str] = None
    client_key_path: Optional[str] = None
    server_name: Optional[str] = None

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "TemporalConnectionSettings":
        values = os.environ if environ is None else environ
        api_key = _optional(values.get("TEMPORAL_API_KEY"))
        cert_path = _optional(values.get("TEMPORAL_CLIENT_CERT"))
        key_path = _optional(values.get("TEMPORAL_CLIENT_KEY"))
        if (cert_path is None) != (key_path is None):
            raise ValueError(
                "TEMPORAL_CLIENT_CERT and TEMPORAL_CLIENT_KEY must be set together"
            )
        tls_enabled = _boolean(
            values.get("TEMPORAL_TLS"),
            default=bool(api_key or cert_path),
        )
        if (api_key or cert_path) and not tls_enabled:
            raise ValueError("Temporal authentication requires TLS")
        if api_key and cert_path:
            raise ValueError(
                "configure either TEMPORAL_API_KEY or mTLS files, not both"
            )
        return cls(
            address=values.get("TEMPORAL_ADDRESS", "localhost:7233").strip(),
            namespace=values.get("TEMPORAL_NAMESPACE", "default").strip(),
            task_queue=values.get(
                "TEMPORAL_TASK_QUEUE",
                TEMPORAL_TASK_QUEUE,
            ).strip(),
            tls_enabled=tls_enabled,
            api_key=api_key,
            client_cert_path=cert_path,
            client_key_path=key_path,
            server_name=_optional(values.get("TEMPORAL_SERVER_NAME")),
        ).validated()

    def validated(self) -> "TemporalConnectionSettings":
        for field_name, value in (
            ("address", self.address),
            ("namespace", self.namespace),
            ("task_queue", self.task_queue),
        ):
            if not value:
                raise ValueError(f"Temporal {field_name} must not be empty")
        return self

    def tls_config(self):
        if not self.tls_enabled:
            return False
        if self.client_cert_path is None:
            return True
        assert self.client_key_path is not None
        return TLSConfig(
            domain=self.server_name,
            client_cert=_read_secret_file(
                self.client_cert_path,
                "Temporal client certificate",
            ),
            client_private_key=_read_secret_file(
                self.client_key_path,
                "Temporal client private key",
            ),
        )


async def connect_temporal(
    settings: TemporalConnectionSettings,
) -> Client:
    settings.validated()
    return await Client.connect(
        settings.address,
        namespace=settings.namespace,
        api_key=settings.api_key,
        tls=settings.tls_config(),
    )


def _read_secret_file(path: str, label: str) -> bytes:
    try:
        content = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"could not read {label} file {path!r}: {exc}") from exc
    if not content.strip():
        raise ValueError(f"{label} file {path!r} is empty")
    return content

