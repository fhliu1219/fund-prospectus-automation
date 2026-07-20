"""AWS S3 implementation of the immutable artifact-store contract."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .artifact_store import (
    ArtifactIntegrityError,
    StoredArtifact,
)


class S3ArtifactStore:
    """Persist content-addressed objects with explicit encryption and SHA-256."""

    provider = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str = "prospectus-artifacts",
        encryption: Optional[str] = None,
        kms_key_id: Optional[str] = None,
        bucket_key_enabled: bool = True,
        client=None,
    ) -> None:
        self.bucket = bucket.strip()
        if not self.bucket:
            raise ValueError("S3 bucket must not be empty")
        self.prefix = prefix.strip("/")
        if any(part == ".." for part in self.prefix.split("/")):
            raise ValueError("S3 prefix must not contain '..'")
        if encryption not in {"AES256", "aws:kms"}:
            raise ValueError(
                "encryption must be explicitly set to 'AES256' or 'aws:kms'"
            )
        if encryption == "aws:kms" and not kms_key_id:
            raise ValueError("kms_key_id is required for aws:kms encryption")
        if encryption == "AES256" and kms_key_id is not None:
            raise ValueError("kms_key_id is valid only for aws:kms encryption")
        self.encryption = encryption
        self.kms_key_id = kms_key_id
        self.bucket_key_enabled = bucket_key_enabled
        self.client = client or boto3.client("s3")

    def put_file(
        self,
        source_path: Path,
        expected_size: int,
        expected_sha256: str,
        content_type: Optional[str] = None,
    ) -> StoredArtifact:
        digest = _normalize_digest(expected_sha256)
        _verify_local_source(source_path, expected_size, digest)
        key_suffix = f"sha256/{digest[:2]}/{digest}"
        object_key = (
            f"{self.prefix}/{key_suffix}" if self.prefix else key_suffix
        )
        artifact = StoredArtifact(
            storage_provider=self.provider,
            storage_namespace=self.bucket,
            object_key=object_key,
            size_bytes=expected_size,
            sha256=digest,
            content_type=content_type,
        )

        head = self._head_or_none(object_key)
        if head is not None:
            self._verify_head(artifact, head)
            return artifact

        request = {
            "Bucket": self.bucket,
            "Key": object_key,
            "ContentLength": expected_size,
            "ChecksumSHA256": _base64_sha256(digest),
            "IfNoneMatch": "*",
            "Metadata": {"sha256": digest},
            "ServerSideEncryption": self.encryption,
        }
        if content_type:
            request["ContentType"] = content_type
        if self.encryption == "aws:kms":
            request["SSEKMSKeyId"] = self.kms_key_id
            request["BucketKeyEnabled"] = self.bucket_key_enabled

        try:
            with source_path.open("rb") as source:
                self.client.put_object(Body=source, **request)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            if code in {
                "PreconditionFailed",
                "ConditionalRequestConflict",
            } or status in {409, 412}:
                self.verify(artifact)
                return artifact
            raise ArtifactIntegrityError(
                f"could not upload S3 artifact s3://{self.bucket}/{object_key}: "
                f"{exc}"
            ) from exc
        except (OSError, BotoCoreError) as exc:
            raise ArtifactIntegrityError(
                f"could not upload S3 artifact s3://{self.bucket}/{object_key}: "
                f"{exc}"
            ) from exc
        self.verify(artifact)
        return artifact

    def verify(self, artifact: StoredArtifact) -> None:
        if artifact.storage_provider != self.provider:
            raise ArtifactIntegrityError(
                f"artifact provider {artifact.storage_provider!r} does not match "
                f"{self.provider!r}"
            )
        if artifact.storage_namespace != self.bucket:
            raise ArtifactIntegrityError(
                f"artifact bucket {artifact.storage_namespace!r} does not match "
                f"{self.bucket!r}"
            )
        head = self._head_or_none(artifact.object_key)
        if head is None:
            raise ArtifactIntegrityError(
                f"S3 artifact does not exist: "
                f"s3://{self.bucket}/{artifact.object_key}"
            )
        self._verify_head(artifact, head)

    def _head_or_none(self, object_key: str) -> Optional[dict]:
        try:
            return self.client.head_object(
                Bucket=self.bucket,
                Key=object_key,
                ChecksumMode="ENABLED",
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return None
            raise ArtifactIntegrityError(
                f"could not inspect S3 artifact "
                f"s3://{self.bucket}/{object_key}: {exc}"
            ) from exc
        except BotoCoreError as exc:
            raise ArtifactIntegrityError(
                f"could not inspect S3 artifact "
                f"s3://{self.bucket}/{object_key}: {exc}"
            ) from exc

    def _verify_head(self, artifact: StoredArtifact, head: dict) -> None:
        if int(head.get("ContentLength", -1)) != artifact.size_bytes:
            raise ArtifactIntegrityError(
                f"S3 artifact size mismatch for "
                f"s3://{self.bucket}/{artifact.object_key}"
            )
        metadata_digest = (
            head.get("Metadata", {}).get("sha256", "").strip().lower()
        )
        if metadata_digest != artifact.sha256:
            raise ArtifactIntegrityError(
                f"S3 artifact metadata checksum mismatch for "
                f"s3://{self.bucket}/{artifact.object_key}"
            )
        checksum = head.get("ChecksumSHA256")
        if checksum != _base64_sha256(artifact.sha256):
            raise ArtifactIntegrityError(
                f"S3 artifact SHA-256 mismatch for "
                f"s3://{self.bucket}/{artifact.object_key}"
            )
        if head.get("ServerSideEncryption") != self.encryption:
            raise ArtifactIntegrityError(
                f"S3 artifact encryption mismatch for "
                f"s3://{self.bucket}/{artifact.object_key}"
            )


def _normalize_digest(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64:
        raise ArtifactIntegrityError(f"invalid SHA-256 digest {value!r}")
    try:
        bytes.fromhex(normalized)
    except ValueError as exc:
        raise ArtifactIntegrityError(
            f"invalid SHA-256 digest {value!r}"
        ) from exc
    return normalized


def _base64_sha256(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def _verify_local_source(
    source_path: Path,
    expected_size: int,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        with source_path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ArtifactIntegrityError(
            f"could not read artifact source {source_path}: {exc}"
        ) from exc
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise ArtifactIntegrityError(
            f"artifact integrity mismatch for {source_path}: expected "
            f"{expected_size} bytes/{expected_sha256}, received "
            f"{size} bytes/{digest.hexdigest()}"
        )
