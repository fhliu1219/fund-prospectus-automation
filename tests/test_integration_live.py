"""Optional live integration test against real SEC EDGAR.

Skipped by default so the suite stays offline and deterministic. Run with:

    RUN_LIVE_TESTS=1 pytest tests/test_integration_live.py
"""

import os

import pytest

from prospectus_fetcher.edgar import EdgarClient, base_form
from prospectus_fetcher.downloader import Downloader
from prospectus_fetcher.models import (
    DocumentKind,
    DocumentRole,
    DocumentVerification,
    IdentityLevel,
)
from prospectus_fetcher.package import DocumentPackageBuilder
from prospectus_fetcher.resolver import Resolver
from prospectus_fetcher.sec_client import SECClient
from prospectus_fetcher.sec_schema import validate_archive_index

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="set RUN_LIVE_TESTS=1 to run live EDGAR tests",
)


def test_live_edgar_identity_and_document_packages(tmp_path):
    client = SECClient()
    resolver = Resolver(client)
    edgar = EdgarClient(client, resolver)
    package_builder = DocumentPackageBuilder(
        edgar, Downloader(client, output_dir=str(tmp_path))
    )

    fund = resolver.resolve("VUSXX")
    assert fund is not None and fund.series_id and fund.class_id

    filing = edgar.find_prospectus(fund)
    assert filing is not None
    assert base_form(filing.form) in {"497K", "485BPOS", "485APOS", "N-1A", "497"}
    assert filing.identity_level is IdentityLevel.CLASS
    assert filing.document_verification is DocumentVerification.NOT_CHECKED
    assert filing.class_id == fund.class_id
    assert fund.class_id in filing.identity_evidence[0]
    # archive path uses the registrant CIK (891190), never the accession prefix
    assert filing.doc_url.startswith("https://www.sec.gov/Archives/edgar/data/891190/")

    vusxx_result = package_builder.build(fund, filing)
    assert vusxx_result.document_verification is DocumentVerification.VERIFIED
    assert vusxx_result.document_kind in {
        DocumentKind.SUMMARY_PROSPECTUS,
        DocumentKind.STATUTORY_PROSPECTUS,
        DocumentKind.SUPPLEMENT,
    }
    vusxx_index_url = vusxx_result.documents[0].archive_index_url
    assert vusxx_index_url
    assert validate_archive_index(
        client.get_json(vusxx_index_url), vusxx_index_url
    )

    qqq = resolver.resolve("QQQ")
    assert qqq is not None and qqq.class_id
    qqq_filing = edgar.find_prospectus(qqq)
    assert qqq_filing is not None
    qqq_inventory = edgar.accession_document_inventory(qqq_filing)
    assert any(
        document.url == qqq_filing.doc_url and document.eligible
        for document in qqq_inventory.documents
    )
    qqq_result = package_builder.build(qqq, qqq_filing)
    assert qqq_result.identity_level is IdentityLevel.CLASS
    assert qqq_result.document_verification is DocumentVerification.VERIFIED
    if qqq_result.document_kind is DocumentKind.SUPPLEMENT:
        assert [document.role for document in qqq_result.documents] == [
            DocumentRole.SUPPLEMENT,
            DocumentRole.BASE_PROSPECTUS,
        ]
    else:
        assert qqq_result.documents[0].role is DocumentRole.PRIMARY_PROSPECTUS

    spy = resolver.resolve("SPY")
    assert spy is not None and not spy.class_id and not spy.series_id
    spy_filing = edgar.find_prospectus(spy)
    assert spy_filing is not None
    spy_result = package_builder.build(spy, spy_filing)
    assert spy_result.identity_level is IdentityLevel.REGISTRANT
    assert spy_result.document_kind is DocumentKind.STATUTORY_PROSPECTUS
    assert spy_result.document_verification is DocumentVerification.VERIFIED
