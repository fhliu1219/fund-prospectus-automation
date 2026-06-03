"""Optional, best-effort HTML -> PDF conversion.

This is off the critical path: it never raises. If WeasyPrint (which has heavy
system dependencies) isn't available, or conversion fails, we log a warning and
carry on — the HTML download is always the real deliverable.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def to_pdf(html_path: str) -> Optional[str]:
    """Convert a saved HTML prospectus to a sibling .pdf. Returns the path or None."""
    pdf_path = html_path.rsplit(".", 1)[0] + ".pdf"
    try:
        from weasyprint import HTML  # imported lazily; optional dependency
    except Exception:
        logger.warning(
            "PDF conversion skipped: WeasyPrint is not available. "
            "Install it (or use the Docker image) to enable --pdf."
        )
        return None
    try:
        HTML(filename=html_path).write_pdf(pdf_path)
        logger.info("Wrote PDF -> %s", pdf_path)
        return pdf_path
    except Exception as exc:  # never let PDF issues break the run
        logger.warning("PDF conversion failed for %s: %s", html_path, exc)
        return None
