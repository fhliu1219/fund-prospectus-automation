"""Immutable artifact storage contracts and a local content-addressed adapter."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArtifactIntegrityError(ValueError):
    """Artifact bytes or metadata violated the immutable storage contract."""


@dataclass(frozen=True)
class StoredArtifact:
    storage_provider: str
    storage_namespace: Optional[str]
    object_key: str
    size_bytes: int
    sha256: str
    content_type: Optional[str]


@dataclass(frozen=True)
class PreparedArtifact:
    role: str
    accession: str
    source_url: Optional[str]
    stored: StoredArtifact


class ArtifactStore(Protocol):
    def put_file(
        self,
        source_path: Path,
        expected_size: int,
        expected_sha256: str,
        content_type: Optional[str] = None,
    ) -> StoredArtifact:
        ...

    def verify(self, artifact: StoredArtifact) -> None:
        ...


class LocalContentAddressedArtifactStore:
    """Store immutable local objects under keys derived from their SHA-256."""

    provider = "local-cas"

    def __init__(self, root: Path, namespace: str = "local") -> None:
        self.root = root.resolve()
        self.namespace = namespace.strip()
        if not self.namespace:
            raise ValueError("artifact namespace must not be empty")
        self.root.mkdir(parents=True, exist_ok=True)

    def put_file(
        self,
        source_path: Path,
        expected_size: int,
        expected_sha256: str,
        content_type: Optional[str] = None,
    ) -> StoredArtifact:
        digest = _normalize_sha256(expected_sha256)
        if expected_size < 0:
            raise ArtifactIntegrityError("expected_size must not be negative")
        object_key = f"sha256/{digest[:2]}/{digest}"
        target = self._path_for_key(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            _verify_path(target, expected_size, digest)
        else:
            self._copy_verified(source_path, target, expected_size, digest)

        artifact = StoredArtifact(
            storage_provider=self.provider,
            storage_namespace=self.namespace,
            object_key=object_key,
            size_bytes=expected_size,
            sha256=digest,
            content_type=content_type,
        )
        self.verify(artifact)
        return artifact

    def verify(self, artifact: StoredArtifact) -> None:
        if artifact.storage_provider != self.provider:
            raise ArtifactIntegrityError(
                f"artifact provider {artifact.storage_provider!r} does not match "
                f"{self.provider!r}"
            )
        if artifact.storage_namespace != self.namespace:
            raise ArtifactIntegrityError(
                f"artifact namespace {artifact.storage_namespace!r} does not "
                f"match {self.namespace!r}"
            )
        _verify_path(
            self._path_for_key(artifact.object_key),
            artifact.size_bytes,
            _normalize_sha256(artifact.sha256),
        )

    def path_for(self, artifact: StoredArtifact) -> Path:
        self.verify(artifact)
        return self._path_for_key(artifact.object_key)

    def _path_for_key(self, object_key: str) -> Path:
        candidate = (self.root / object_key).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactIntegrityError(
                f"artifact object key escapes storage root: {object_key!r}"
            ) from exc
        return candidate

    @staticmethod
    def _copy_verified(
        source_path: Path,
        target: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        digest = hashlib.sha256()
        size = 0
        temp_path: Optional[Path] = None
        try:
            with source_path.open("rb") as source:
                descriptor, raw_temp_path = tempfile.mkstemp(
                    prefix=".artifact-",
                    dir=str(target.parent),
                )
                temp_path = Path(raw_temp_path)
                with os.fdopen(descriptor, "wb") as destination:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        destination.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
        except OSError as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise ArtifactIntegrityError(
                f"could not persist artifact {source_path}: {exc}"
            ) from exc

        actual_sha256 = digest.hexdigest()
        if size != expected_size or actual_sha256 != expected_sha256:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise ArtifactIntegrityError(
                f"artifact integrity mismatch for {source_path}: expected "
                f"{expected_size} bytes/{expected_sha256}, received "
                f"{size} bytes/{actual_sha256}"
            )

        assert temp_path is not None
        try:
            os.link(str(temp_path), str(target))
            temp_path.unlink()
        except FileExistsError:
            temp_path.unlink(missing_ok=True)
            _verify_path(target, expected_size, expected_sha256)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise ArtifactIntegrityError(
                f"could not publish artifact {target}: {exc}"
            ) from exc


def prepare_manifest_artifacts(
    manifest: dict,
    artifact_store: ArtifactStore,
) -> List[PreparedArtifact]:
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise ArtifactIntegrityError("manifest.documents must be an array")

    prepared: List[PreparedArtifact] = []
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise ArtifactIntegrityError(
                f"manifest.documents[{index}] must be an object"
            )
        required = ("role", "accession", "path", "size_bytes", "sha256")
        missing = [name for name in required if document.get(name) in {None, ""}]
        if missing:
            raise ArtifactIntegrityError(
                f"manifest.documents[{index}] missing {', '.join(missing)}"
            )

        path = Path(str(document["path"]))
        content_type = document.get("content_type")
        if content_type is None:
            content_type = mimetypes.guess_type(path.name)[0]
        stored = artifact_store.put_file(
            path,
            int(document["size_bytes"]),
            str(document["sha256"]),
            content_type=content_type,
        )
        prepared.append(
            PreparedArtifact(
                role=str(document["role"]),
                accession=str(document["accession"]),
                source_url=(
                    str(document["source_url"])
                    if document.get("source_url")
                    else None
                ),
                stored=stored,
            )
        )
    return prepared


def _normalize_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ArtifactIntegrityError(
            f"invalid SHA-256 digest {value!r}"
        )
    return normalized


def _verify_path(path: Path, expected_size: int, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ArtifactIntegrityError(
            f"could not read stored artifact {path}: {exc}"
        ) from exc

    actual_sha256 = digest.hexdigest()
    if size != expected_size or actual_sha256 != expected_sha256:
        raise ArtifactIntegrityError(
            f"stored artifact integrity mismatch for {path}: expected "
            f"{expected_size} bytes/{expected_sha256}, received "
            f"{size} bytes/{actual_sha256}"
        )
