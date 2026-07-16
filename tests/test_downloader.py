"""Downloader tests: path construction, form sanitisation, file writing."""

import json

import responses

from prospectus_fetcher.downloader import Downloader, safe_form
from prospectus_fetcher.models import Filing
from prospectus_fetcher.sec_client import SECClient


def test_safe_form_sanitizes_slash():
    assert safe_form("497K/A") == "497K-A"
    assert safe_form("N-1A/A") == "N-1A-A"
    assert safe_form("485BPOS") == "485BPOS"


@responses.activate
def test_save_writes_document_at_expected_path(tmp_path, monkeypatch):
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", lambda s: None)
    url = "https://www.sec.gov/Archives/edgar/data/891190/000119312525325229/f43673d1.htm"
    responses.add(responses.GET, url, body="<html>prospectus body</html>", status=200)

    filing = Filing(
        registrant_cik=891190,
        accession="0001193125-25-325229",
        form="497K",
        date="2025-12-19",
        doc_url=url,
    )
    path = Downloader(SECClient(), output_dir=str(tmp_path)).save(filing, "vusxx")

    assert path.endswith("VUSXX/2025-12-19_497K_0001193125-25-325229.html")
    with open(path) as fh:
        assert "prospectus body" in fh.read()


@responses.activate
def test_save_sanitizes_amended_form_in_filename(tmp_path, monkeypatch):
    monkeypatch.setattr("prospectus_fetcher.sec_client.time.sleep", lambda s: None)
    url = "https://www.sec.gov/Archives/edgar/data/1/2/x.htm"
    responses.add(responses.GET, url, body="x", status=200)

    filing = Filing(
        registrant_cik=1, accession="0001-25-000001", form="497K/A", date="2025-12-19", doc_url=url
    )
    path = Downloader(SECClient(), output_dir=str(tmp_path)).save(filing, "ZZZ")

    assert "497K-A" in path
    assert "/" not in path.rsplit("/", 1)[-1].replace(".html", "")  # filename has no stray slash


def test_save_manifest_writes_deterministic_json(tmp_path):
    downloader = Downloader(SECClient(), output_dir=str(tmp_path))

    path = downloader.save_manifest("vusxx", {"ticker": "VUSXX", "version": 1})

    with open(path, encoding="utf-8") as handle:
        assert json.load(handle) == {"ticker": "VUSXX", "version": 1}
    assert not (tmp_path / "VUSXX" / "manifest.json.tmp").exists()
