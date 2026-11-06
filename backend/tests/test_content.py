"""GET /content/{file} when the CDN is off."""
import boto3
import pytest
from moto import mock_aws

from taalpunt_api import content


@pytest.fixture
def bucket(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-north-1")
        s3.create_bucket(Bucket="content", CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        s3.put_object(Bucket="content", Key="content/manifest.json", Body=b'{"content_version":"1"}',
                      CacheControl="public, max-age=60")
        s3.put_object(Bucket="content", Key="content/knm-2026-11-05.1.json", Body=b'{"units":[]}')
        monkeypatch.setenv("CONTENT_BUCKET", "content")
        monkeypatch.setattr(content, "_s3", s3)
        yield


def get(name):
    return content.handler({"pathParameters": {"file": name}}, None)


def test_serves_manifest_and_bundle(bucket):
    res = get("manifest.json")
    assert res["statusCode"] == 200 and res["body"] == '{"content_version":"1"}'
    assert res["headers"]["cache-control"] == "public, max-age=60"
    assert get("knm-2026-11-05.1.json")["statusCode"] == 200


@pytest.mark.parametrize("name", ["../secret.json", "other.json", "manifest.txt", "", "knm-x.json"])
def test_only_content_files(bucket, name):
    assert get(name)["statusCode"] == 404
