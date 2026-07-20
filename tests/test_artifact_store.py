"""Immutable local artifact-store contract tests."""

import hashlib

import pytest

from prospectus_fetcher.artifact_store import (
    ArtifactIntegrityError,
    LocalContentAddressedArtifactStore,
    StoredArtifact,
)


def test_local_store_uses_content_address_and_deduplicates_bytes(tmp_path):
    store = LocalContentAddressedArtifactStore(tmp_path / "objects")
    first_path = tmp_path / "first.html"
    second_path = tmp_path / "second.html"
    content = b"<html>same prospectus</html>"
    first_path.write_bytes(content)
    second_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    first = store.put_file(first_path, len(content), digest, "text/html")
    second = store.put_file(second_path, len(content), digest, "text/html")

    assert first == second
    assert first.object_key == f"sha256/{digest[:2]}/{digest}"
    assert store.path_for(first).read_bytes() == content
    stored_files = [
        path for path in (tmp_path / "objects").rglob("*") if path.is_file()
    ]
    assert len(stored_files) == 1


def test_local_store_rejects_source_checksum_drift_without_publishing(tmp_path):
    store = LocalContentAddressedArtifactStore(tmp_path / "objects")
    source = tmp_path / "prospectus.html"
    source.write_bytes(b"changed")

    with pytest.raises(ArtifactIntegrityError, match="integrity mismatch"):
        store.put_file(source, 7, "0" * 64, "text/html")

    assert not [
        path for path in (tmp_path / "objects").rglob("*") if path.is_file()
    ]


def test_local_store_detects_corruption_after_publication(tmp_path):
    store = LocalContentAddressedArtifactStore(tmp_path / "objects")
    source = tmp_path / "prospectus.html"
    content = b"verified"
    source.write_bytes(content)
    artifact = store.put_file(
        source,
        len(content),
        hashlib.sha256(content).hexdigest(),
    )
    store.path_for(artifact).write_bytes(b"corrupt")

    with pytest.raises(ArtifactIntegrityError, match="stored artifact"):
        store.verify(artifact)


def test_local_store_rejects_object_key_traversal(tmp_path):
    store = LocalContentAddressedArtifactStore(tmp_path / "objects")
    artifact = StoredArtifact(
        storage_provider="local-cas",
        storage_namespace="local",
        object_key="../../outside",
        size_bytes=0,
        sha256="0" * 64,
        content_type=None,
    )

    with pytest.raises(ArtifactIntegrityError, match="escapes storage root"):
        store.verify(artifact)
