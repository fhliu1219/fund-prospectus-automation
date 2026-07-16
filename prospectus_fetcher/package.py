"""Assemble validated prospectus documents and their evidence manifest."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from .converter import to_pdf
from .downloader import Downloader
from .edgar import EdgarClient
from .models import (
    CandidateDisposition,
    CandidatePurpose,
    CandidateSearchAudit,
    DocumentArtifact,
    DocumentCandidateEvaluation,
    DocumentKind,
    DocumentRole,
    DocumentVerification,
    FetchResult,
    Filing,
    IdentityLevel,
    ResolvedFund,
)
from .validator import DocumentValidator, ValidationResult


@dataclass
class _SiblingRecovery:
    filing: Filing
    content: bytes
    validation: ValidationResult
    warnings: List[str]
    candidates: List[DocumentCandidateEvaluation]
    search: CandidateSearchAudit


@dataclass
class _BaseSearch:
    base: Optional[Tuple[Filing, bytes, ValidationResult, List[str]]]
    warnings: List[str]
    candidates: List[DocumentCandidateEvaluation]
    search: CandidateSearchAudit


class DocumentPackageBuilder:
    """Validate selected content and include a verified base for supplements."""

    def __init__(
        self,
        edgar: EdgarClient,
        downloader: Downloader,
        validator: Optional[DocumentValidator] = None,
        want_pdf: bool = False,
    ) -> None:
        self.edgar = edgar
        self.downloader = downloader
        self.validator = validator or DocumentValidator()
        self.want_pdf = want_pdf

    def build(self, fund: ResolvedFund, selected: Filing) -> FetchResult:
        ticker = fund.ticker.upper()
        selection_warnings = list(selected.warnings)
        content = self.downloader.download(selected, ticker)
        validation = self.validator.validate(content, ticker, fund.class_id)
        self._apply_validation(selected, validation)

        candidate_evaluations: List[DocumentCandidateEvaluation] = []
        candidate_searches: List[CandidateSearchAudit] = []
        recovery_warnings: List[str] = []
        if not self._is_usable_selected_document(validation):
            recovery = self._recover_sibling(
                fund,
                selected,
                content,
                validation,
            )
            selected = recovery.filing
            content = recovery.content
            validation = recovery.validation
            recovery_warnings.extend(recovery.warnings)
            candidate_evaluations.extend(recovery.candidates)
            candidate_searches.append(recovery.search)

        path = self.downloader.save_content(selected, ticker, content)
        selected_role = (
            DocumentRole.SUPPLEMENT
            if validation.kind is DocumentKind.SUPPLEMENT
            else DocumentRole.PRIMARY_PROSPECTUS
        )
        artifacts = [
            self._artifact(selected, selected_role, path, content, validation)
        ]
        package_verification = validation.verification
        package_evidence = list(validation.evidence)
        package_warnings = selection_warnings + recovery_warnings

        if validation.kind is DocumentKind.SUPPLEMENT:
            base_search = self._find_base(fund, selected, validation)
            package_warnings.extend(base_search.warnings)
            candidate_evaluations.extend(base_search.candidates)
            candidate_searches.append(base_search.search)
            if base_search.base is None:
                package_warnings.extend(validation.warnings)
                package_warnings.append(
                    "no verified complete base prospectus was found in the exhausted "
                    "identity-scoped filing history"
                )
            else:
                base_filing, base_content, base_validation, relationship = base_search.base
                base_path = self.downloader.save_content(base_filing, ticker, base_content)
                artifacts.append(
                    self._artifact(
                        base_filing,
                        DocumentRole.BASE_PROSPECTUS,
                        base_path,
                        base_content,
                        base_validation,
                    )
                )
                package_evidence.extend(base_validation.evidence)
                package_evidence.extend(relationship)
                shared_dates = set(validation.base_prospectus_dates).intersection(
                    base_validation.referenced_dates
                )
                supplement_has_identity = (
                    validation.ticker_found or validation.class_id_found
                ) and not validation.contradictions
                if (
                    shared_dates
                    and supplement_has_identity
                    and not base_validation.contradictions
                ):
                    package_verification = DocumentVerification.VERIFIED
                else:
                    package_verification = DocumentVerification.MANUAL_REVIEW_REQUIRED
                    if not supplement_has_identity:
                        package_warnings.append(
                            "supplement lacks direct evidence for the requested ticker or class"
                        )
                    if not shared_dates:
                        package_warnings.append(
                            "base prospectus covers the ticker, but its relationship to the "
                            "supplement lacks a matching referenced date"
                        )
        else:
            package_warnings.extend(validation.warnings)

        if self.want_pdf:
            for artifact in artifacts:
                to_pdf(artifact.path)

        result = FetchResult(
            ticker=ticker,
            status="ok",
            form=selected.form,
            date=selected.date,
            fund_name=selected.fund_name,
            selection_reason=selected.selection_reason,
            path=path,
            identity_level=selected.identity_level,
            document_kind=validation.kind,
            document_verification=package_verification,
            identity_evidence=list(selected.identity_evidence),
            document_evidence=package_evidence,
            warnings=self._dedupe(package_warnings),
            documents=artifacts,
            candidate_evaluations=candidate_evaluations,
            candidate_searches=candidate_searches,
        )
        result.manifest_path = self.downloader.save_manifest(
            ticker, self._manifest(result, fund)
        )
        return result

    def _recover_sibling(
        self,
        fund: ResolvedFund,
        selected: Filing,
        selected_content: bytes,
        selected_validation: ValidationResult,
    ) -> _SiblingRecovery:
        purpose = CandidatePurpose.PRIMARY_RECOVERY
        search = CandidateSearchAudit(
            purpose=purpose,
            identity_level=selected.identity_level,
            identifier=selected.accession,
            complete=False,
            evaluated_count=1,
        )
        current_name = self._url_name(selected.doc_url)
        candidates = [
            self._candidate_evaluation(
                purpose=purpose,
                disposition=CandidateDisposition.CURRENT_PRIMARY,
                filing=selected,
                name=current_name,
                content=selected_content,
                validation=selected_validation,
                reason="SEC-designated primary document did not satisfy automatic-use rules",
            )
        ]
        warnings: List[str] = []
        try:
            inventory = self.edgar.accession_document_inventory(selected)
        except Exception as exc:
            warning = f"could not inventory accession siblings: {exc}"
            search.stop_reason = warning
            return _SiblingRecovery(
                selected,
                selected_content,
                selected_validation,
                [warning],
                candidates,
                search,
            )

        search.complete = True
        search.discovered_count = len(inventory.documents)
        warnings.extend(inventory.warnings)
        qualified: List[Tuple[Filing, bytes, ValidationResult, int]] = []
        evaluation_errors = 0

        for document in inventory.documents:
            if document.name == current_name:
                continue
            if not document.eligible:
                candidates.append(
                    DocumentCandidateEvaluation(
                        purpose=purpose,
                        disposition=CandidateDisposition.EXCLUDED,
                        accession=selected.accession,
                        name=document.name,
                        source_url=document.url,
                        size_bytes=document.size_bytes,
                        reason=document.exclusion_reason,
                    )
                )
                continue

            search.evaluated_count += 1
            candidate = replace(
                selected,
                doc_url=document.url,
                heuristic_used=False,
                document_kind=DocumentKind.UNKNOWN,
                document_verification=DocumentVerification.NOT_CHECKED,
                document_evidence=[],
                warnings=list(selected.warnings),
            )
            try:
                content = self.downloader.download(candidate, fund.ticker)
                validation = self.validator.validate(content, fund.ticker, fund.class_id)
            except Exception as exc:
                evaluation_errors += 1
                message = f"could not evaluate sibling {document.name}: {exc}"
                warnings.append(message)
                candidates.append(
                    DocumentCandidateEvaluation(
                        purpose=purpose,
                        disposition=CandidateDisposition.ERROR,
                        accession=selected.accession,
                        name=document.name,
                        source_url=document.url,
                        size_bytes=document.size_bytes,
                        reason=message,
                    )
                )
                continue

            self._apply_validation(candidate, validation)
            if self._is_usable_selected_document(validation):
                disposition = CandidateDisposition.QUALIFIED
                reason = "passed direct-identity and document-content recovery rules"
            else:
                disposition = CandidateDisposition.REJECTED
                reason = self._rejection_reason(validation)
            candidates.append(
                self._candidate_evaluation(
                    purpose=purpose,
                    disposition=disposition,
                    filing=candidate,
                    name=document.name,
                    content=content,
                    validation=validation,
                    reason=reason,
                    size_bytes=document.size_bytes,
                )
            )
            if disposition is CandidateDisposition.QUALIFIED:
                qualified.append((candidate, content, validation, len(candidates) - 1))

        if len(qualified) == 1 and evaluation_errors == 0:
            recovered, content, validation, evaluation_index = qualified[0]
            candidates[evaluation_index].disposition = CandidateDisposition.SELECTED
            candidates[evaluation_index].reason = (
                "selected as the only sibling passing direct-identity and "
                "document-content recovery rules"
            )
            message = (
                f"recovered {candidates[evaluation_index].name} as the only verified "
                "prospectus sibling in the selected accession"
            )
            recovered.warnings.append(message)
            warnings.append(message)
            search.stop_reason = "exactly one sibling qualified and was selected"
            return _SiblingRecovery(
                recovered,
                content,
                validation,
                warnings,
                candidates,
                search,
            )

        if evaluation_errors:
            warning = (
                f"{evaluation_errors} eligible accession sibling(s) could not be "
                "evaluated; uniqueness is unproven and manual review is required"
            )
            search.stop_reason = "eligible sibling evaluation failed"
        elif qualified:
            warning = (
                f"{len(qualified)} accession siblings passed recovery rules; "
                "manual review is required"
            )
            search.stop_reason = "multiple siblings qualified"
        else:
            warning = "no accession sibling passed recovery rules"
            search.stop_reason = "no sibling qualified"
        warnings.append(warning)
        return _SiblingRecovery(
            selected,
            selected_content,
            selected_validation,
            warnings,
            candidates,
            search,
        )

    def _find_base(
        self,
        fund: ResolvedFund,
        selected: Filing,
        supplement: ValidationResult,
    ) -> _BaseSearch:
        purpose = CandidatePurpose.SUPPLEMENT_BASE
        search = CandidateSearchAudit(
            purpose=purpose,
            identity_level=selected.identity_level,
            identifier=self._identity_identifier(fund, selected),
            complete=False,
        )
        fallback: Optional[Tuple[Filing, bytes, ValidationResult, List[str]]] = None
        fallback_index: Optional[int] = None
        evaluations: List[DocumentCandidateEvaluation] = []
        warnings: List[str] = []
        try:
            refs = self.edgar.related_prospectus_refs(
                fund,
                selected,
                supplement.base_prospectus_dates,
            )
        except Exception as exc:
            message = f"could not discover base prospectus candidates: {exc}"
            search.stop_reason = message
            return _BaseSearch(None, [message], evaluations, search)

        search.complete = True
        search.discovered_count = len(refs)

        for ref in refs:
            search.evaluated_count += 1
            try:
                candidate = self.edgar.resolve_related_prospectus(fund, selected, ref)
                content = self.downloader.download(candidate, fund.ticker)
                validation = self.validator.validate(
                    content, fund.ticker, fund.class_id
                )
            except Exception as exc:
                message = f"could not evaluate base candidate {ref.accession}: {exc}"
                warnings.append(message)
                evaluations.append(
                    DocumentCandidateEvaluation(
                        purpose=purpose,
                        disposition=CandidateDisposition.ERROR,
                        accession=ref.accession,
                        name=getattr(ref, "primary_document", None) or "(unresolved)",
                        source_url=getattr(ref, "filing_href", None),
                        reason=message,
                    )
                )
                continue
            self._apply_validation(candidate, validation)
            if (
                not validation.complete
                or validation.verification is not DocumentVerification.VERIFIED
                or validation.contradictions
            ):
                evaluations.append(
                    self._candidate_evaluation(
                        purpose=purpose,
                        disposition=CandidateDisposition.REJECTED,
                        filing=candidate,
                        name=self._url_name(candidate.doc_url),
                        content=content,
                        validation=validation,
                        reason=self._rejection_reason(validation),
                    )
                )
                continue

            relationship = self._relationship_evidence(supplement, validation, fund)
            value = (candidate, content, validation, relationship)
            shared_dates = set(supplement.base_prospectus_dates).intersection(
                validation.referenced_dates
            )
            if shared_dates:
                evaluations.append(
                    self._candidate_evaluation(
                        purpose=purpose,
                        disposition=CandidateDisposition.SELECTED,
                        filing=candidate,
                        name=self._url_name(candidate.doc_url),
                        content=content,
                        validation=validation,
                        reason=(
                            "selected verified base matching supplement prospectus "
                            "date(s): " + ", ".join(sorted(shared_dates))
                        ),
                    )
                )
                search.stop_reason = "verified date-linked base found"
                return _BaseSearch(value, warnings, evaluations, search)

            evaluations.append(
                self._candidate_evaluation(
                    purpose=purpose,
                    disposition=CandidateDisposition.QUALIFIED,
                    filing=candidate,
                    name=self._url_name(candidate.doc_url),
                    content=content,
                    validation=validation,
                    reason="verified complete prospectus but no referenced date matched",
                )
            )
            if fallback is None:
                fallback = value
                fallback_index = len(evaluations) - 1

        if fallback is not None and fallback_index is not None:
            evaluations[fallback_index].disposition = CandidateDisposition.SELECTED
            evaluations[fallback_index].reason = (
                "selected as a verified fallback after exhausting history without a "
                "referenced-date match; manual review remains required"
            )
            search.stop_reason = "history exhausted; verified fallback lacks date linkage"
        else:
            search.stop_reason = "history exhausted; no verified complete base found"
        return _BaseSearch(fallback, warnings, evaluations, search)

    @staticmethod
    def _is_usable_selected_document(validation: ValidationResult) -> bool:
        if validation.contradictions:
            return False
        if (
            validation.complete
            and validation.verification is DocumentVerification.VERIFIED
        ):
            return True
        return (
            validation.kind is DocumentKind.SUPPLEMENT
            and (validation.ticker_found or validation.class_id_found)
        )

    @staticmethod
    def _rejection_reason(validation: ValidationResult) -> str:
        if validation.contradictions:
            return "contradictory identity evidence: " + "; ".join(
                validation.contradictions
            )
        if not validation.complete and validation.kind is not DocumentKind.SUPPLEMENT:
            return f"document classified as {validation.kind.value}, not a complete prospectus"
        if validation.kind is DocumentKind.SUPPLEMENT:
            if validation.ticker_found or validation.class_id_found:
                return "supplement is not a complete base prospectus"
            return "supplement lacks direct ticker or class evidence"
        return "complete prospectus lacks sufficient direct ticker or class evidence"

    @staticmethod
    def _candidate_evaluation(
        purpose: CandidatePurpose,
        disposition: CandidateDisposition,
        filing: Filing,
        name: str,
        content: bytes,
        validation: ValidationResult,
        reason: str,
        size_bytes: Optional[int] = None,
    ) -> DocumentCandidateEvaluation:
        return DocumentCandidateEvaluation(
            purpose=purpose,
            disposition=disposition,
            accession=filing.accession,
            name=name,
            source_url=filing.doc_url,
            size_bytes=len(content) if size_bytes is None else size_bytes,
            sha256=hashlib.sha256(content).hexdigest(),
            kind=validation.kind,
            verification=validation.verification,
            reason=reason,
            evidence=list(validation.evidence),
            warnings=list(validation.warnings),
            referenced_dates=list(validation.referenced_dates),
            base_prospectus_dates=list(validation.base_prospectus_dates),
            contradictions=list(validation.contradictions),
        )

    @staticmethod
    def _url_name(url: Optional[str]) -> str:
        if not url:
            return "(unknown)"
        return unquote(urlsplit(url).path.rsplit("/", 1)[-1])

    @staticmethod
    def _identity_identifier(fund: ResolvedFund, selected: Filing) -> str:
        if selected.identity_level is IdentityLevel.CLASS and fund.class_id:
            return fund.class_id
        if selected.identity_level is IdentityLevel.SERIES and fund.series_id:
            return fund.series_id
        return str(fund.cik)

    @staticmethod
    def _relationship_evidence(
        supplement: ValidationResult,
        base: ValidationResult,
        fund: ResolvedFund,
    ) -> List[str]:
        evidence: List[str] = []
        if supplement.ticker_found and base.ticker_found:
            evidence.append(
                f"supplement and base prospectus both contain exact ticker {fund.ticker.upper()}"
            )
        if fund.class_id and supplement.class_id_found and base.class_id_found:
            evidence.append(
                f"supplement and base prospectus both contain class identifier {fund.class_id}"
            )
        shared_dates = sorted(
            set(supplement.base_prospectus_dates).intersection(base.referenced_dates)
        )
        if shared_dates:
            evidence.append(
                "supplement references base prospectus date(s): "
                + ", ".join(shared_dates)
            )
        return evidence

    @staticmethod
    def _apply_validation(filing: Filing, validation: ValidationResult) -> None:
        filing.document_kind = validation.kind
        filing.document_verification = validation.verification
        filing.document_evidence = list(validation.evidence)
        filing.warnings.extend(validation.warnings)

    @staticmethod
    def _artifact(
        filing: Filing,
        role: DocumentRole,
        path: str,
        content: bytes,
        validation: ValidationResult,
    ) -> DocumentArtifact:
        return DocumentArtifact(
            role=role,
            kind=validation.kind,
            accession=filing.accession,
            form=filing.form,
            date=filing.date,
            path=path,
            verification=validation.verification,
            source_url=filing.doc_url,
            archive_index_url=(
                filing.doc_url.rsplit("/", 1)[0] + "/index.json"
                if filing.doc_url
                else None
            ),
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            evidence=list(validation.evidence),
            warnings=list(validation.warnings),
            referenced_dates=list(validation.referenced_dates),
            base_prospectus_dates=list(validation.base_prospectus_dates),
            contradictions=list(validation.contradictions),
        )

    @staticmethod
    def _manifest(result: FetchResult, fund: ResolvedFund) -> Dict[str, object]:
        documents = []
        for artifact in result.documents:
            value = asdict(artifact)
            value["role"] = artifact.role.value
            value["kind"] = artifact.kind.value
            value["verification"] = artifact.verification.value
            documents.append(value)
        candidates = []
        for evaluation in result.candidate_evaluations:
            value = asdict(evaluation)
            value["purpose"] = evaluation.purpose.value
            value["disposition"] = evaluation.disposition.value
            value["kind"] = evaluation.kind.value
            value["verification"] = evaluation.verification.value
            candidates.append(value)
        searches = []
        for search in result.candidate_searches:
            value = asdict(search)
            value["purpose"] = search.purpose.value
            value["identity_level"] = search.identity_level.value
            searches.append(value)
        return {
            "schema_version": 2,
            "ticker": result.ticker,
            "selection": {
                "form": result.form,
                "filing_date": result.date,
                "reason": result.selection_reason,
            },
            "identity": {
                "level": result.identity_level.value,
                "registrant_cik": fund.cik,
                "series_id": fund.series_id,
                "class_id": fund.class_id,
                "mapping_source": fund.source,
                "evidence": result.identity_evidence,
            },
            "package": {
                "kind": result.document_kind.value,
                "verification": result.document_verification.value,
                "evidence": result.document_evidence,
                "warnings": result.warnings,
            },
            "documents": documents,
            "recovery": {
                "searches": searches,
                "candidates": candidates,
            },
        }

    @staticmethod
    def _dedupe(values: List[str]) -> List[str]:
        return list(dict.fromkeys(values))
