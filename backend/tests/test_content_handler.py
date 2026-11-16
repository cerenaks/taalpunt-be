"""GET /content/{file} (content.py): a file that doesn't exist answers 404, also when S3 says AccessDenied."""
import pytest

from taalpunt_api import content


class _Missing(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _S3:
    def __init__(self, code):
        self.code = code

    def get_object(self, **_):
        raise _Missing(self.code)


@pytest.mark.parametrize("code", ["NoSuchKey", "AccessDenied"])
def test_missing_file_is_404(monkeypatch, code):
    monkeypatch.setenv("CONTENT_BUCKET", "bucket")
    monkeypatch.setattr(content, "_s3", _S3(code))
    resp = content.handler({"pathParameters": {"file": "knm-2026-11-05.1.json"}}, None)
    assert resp["statusCode"] == 404


def test_other_errors_still_fail(monkeypatch):
    monkeypatch.setenv("CONTENT_BUCKET", "bucket")
    monkeypatch.setattr(content, "_s3", _S3("SlowDown"))
    with pytest.raises(_Missing):
        content.handler({"pathParameters": {"file": "manifest.json"}}, None)
