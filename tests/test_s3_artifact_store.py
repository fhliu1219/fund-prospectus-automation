"""S3 artifact-store contract tests with botocore request stubs."""

import base64
import hashlib

import boto3
import pytest
from botocore.stub import ANY, Stubber

from prospectus_fetcher.artifact_store import ArtifactIntegrityError
from prospectus_fetcher.s3_artifact_store import S3ArtifactStore


def _client():
    return boto3.client(
        "s3",
        region_name="us-west-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _checksum(content):
    digest = hashlib.sha256(content).hexdigest()
    encoded = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
    return digest, encoded


def _head(content, digest, encoded, encryption="AES256"):
    return {
        "ContentLength": len(content),
        "Metadata": {"sha256": digest},
        "ChecksumSHA256": encoded,
        "ServerSideEncryption": encryption,
    }


def test_s3_store_uploads_with_checksum_encryption_and_content_address(tmp_path):
    client = _client()
    stubber = Stubber(client)
    content = b"<html>prospectus</html>"
    digest, encoded = _checksum(content)
    key = f"evidence/sha256/{digest[:2]}/{digest}"
    source = tmp_path / "prospectus.html"
    source.write_bytes(content)
    head_params = {
        "Bucket": "test-bucket",
        "Key": key,
        "ChecksumMode": "ENABLED",
    }
    stubber.add_client_error(
        "head_object",
        service_error_code="404",
        http_status_code=404,
        expected_params=head_params,
    )
    stubber.add_response(
        "put_object",
        {},
        {
            "Body": ANY,
            "Bucket": "test-bucket",
            "Key": key,
            "ContentLength": len(content),
            "ChecksumSHA256": encoded,
            "IfNoneMatch": "*",
            "Metadata": {"sha256": digest},
            "ServerSideEncryption": "AES256",
            "ContentType": "text/html",
        },
    )
    stubber.add_response(
        "head_object",
        _head(content, digest, encoded),
        head_params,
    )

    with stubber:
        store = S3ArtifactStore(
            "test-bucket",
            prefix="evidence",
            encryption="AES256",
            client=client,
        )
        artifact = store.put_file(
            source,
            len(content),
            digest,
            "text/html",
        )

    assert artifact.storage_provider == "s3"
    assert artifact.storage_namespace == "test-bucket"
    assert artifact.object_key == key


def test_s3_store_reuses_an_existing_verified_object(tmp_path):
    client = _client()
    stubber = Stubber(client)
    content = b"same bytes"
    digest, encoded = _checksum(content)
    key = f"sha256/{digest[:2]}/{digest}"
    source = tmp_path / "prospectus.html"
    source.write_bytes(content)
    stubber.add_response(
        "head_object",
        _head(content, digest, encoded),
        {
            "Bucket": "test-bucket",
            "Key": key,
            "ChecksumMode": "ENABLED",
        },
    )

    with stubber:
        artifact = S3ArtifactStore(
            "test-bucket",
            prefix="",
            encryption="AES256",
            client=client,
        ).put_file(source, len(content), digest)

    assert artifact.object_key == key


def test_s3_store_refuses_an_existing_object_with_wrong_checksum(tmp_path):
    client = _client()
    stubber = Stubber(client)
    content = b"expected"
    digest, encoded = _checksum(content)
    key = f"sha256/{digest[:2]}/{digest}"
    source = tmp_path / "prospectus.html"
    source.write_bytes(content)
    response = _head(content, digest, encoded)
    response["Metadata"] = {"sha256": "0" * 64}
    stubber.add_response(
        "head_object",
        response,
        {
            "Bucket": "test-bucket",
            "Key": key,
            "ChecksumMode": "ENABLED",
        },
    )

    with stubber, pytest.raises(
        ArtifactIntegrityError,
        match="metadata checksum",
    ):
        S3ArtifactStore(
            "test-bucket",
            prefix="",
            encryption="AES256",
            client=client,
        ).put_file(source, len(content), digest)


def test_s3_store_accepts_matching_winner_of_a_concurrent_conditional_put(
    tmp_path,
):
    client = _client()
    stubber = Stubber(client)
    content = b"concurrent"
    digest, encoded = _checksum(content)
    key = f"sha256/{digest[:2]}/{digest}"
    source = tmp_path / "prospectus.html"
    source.write_bytes(content)
    head_params = {
        "Bucket": "test-bucket",
        "Key": key,
        "ChecksumMode": "ENABLED",
    }
    stubber.add_client_error(
        "head_object",
        service_error_code="404",
        http_status_code=404,
        expected_params=head_params,
    )
    stubber.add_client_error(
        "put_object",
        service_error_code="PreconditionFailed",
        http_status_code=412,
        expected_params={
            "Body": ANY,
            "Bucket": "test-bucket",
            "Key": key,
            "ContentLength": len(content),
            "ChecksumSHA256": encoded,
            "IfNoneMatch": "*",
            "Metadata": {"sha256": digest},
            "ServerSideEncryption": "AES256",
        },
    )
    stubber.add_response(
        "head_object",
        _head(content, digest, encoded),
        head_params,
    )

    with stubber:
        artifact = S3ArtifactStore(
            "test-bucket",
            prefix="",
            encryption="AES256",
            client=client,
        ).put_file(source, len(content), digest)

    assert artifact.object_key == key


def test_s3_store_requires_an_explicit_encryption_policy():
    with pytest.raises(ValueError, match="explicitly"):
        S3ArtifactStore("test-bucket", client=_client())


def test_s3_store_passes_explicit_kms_configuration(tmp_path):
    client = _client()
    stubber = Stubber(client)
    content = b"kms protected"
    digest, encoded = _checksum(content)
    key = f"sha256/{digest[:2]}/{digest}"
    source = tmp_path / "prospectus.html"
    source.write_bytes(content)
    head_params = {
        "Bucket": "test-bucket",
        "Key": key,
        "ChecksumMode": "ENABLED",
    }
    stubber.add_client_error(
        "head_object",
        service_error_code="404",
        http_status_code=404,
        expected_params=head_params,
    )
    stubber.add_response(
        "put_object",
        {},
        {
            "Body": ANY,
            "Bucket": "test-bucket",
            "Key": key,
            "ContentLength": len(content),
            "ChecksumSHA256": encoded,
            "IfNoneMatch": "*",
            "Metadata": {"sha256": digest},
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": "alias/prospectus",
            "BucketKeyEnabled": True,
        },
    )
    stubber.add_response(
        "head_object",
        _head(content, digest, encoded, encryption="aws:kms"),
        head_params,
    )

    with stubber:
        artifact = S3ArtifactStore(
            "test-bucket",
            prefix="",
            encryption="aws:kms",
            kms_key_id="alias/prospectus",
            client=client,
        ).put_file(source, len(content), digest)

    assert artifact.storage_namespace == "test-bucket"


def test_s3_store_requires_explicit_kms_key_for_kms_encryption():
    with pytest.raises(ValueError, match="kms_key_id"):
        S3ArtifactStore(
            "test-bucket",
            encryption="aws:kms",
            client=_client(),
        )
