FROM python:3.12-slim

# System libraries for WeasyPrint (PDF output via --pdf). The core HTML
# retrieval needs none of these; they're here so --pdf "just works" in the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi8 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt weasyprint

COPY prospectus_fetcher/ ./prospectus_fetcher/
COPY main.py .

# `docker run --rm -v "$PWD/output:/app/output" prospectus VUSXX`
ENTRYPOINT ["python", "main.py"]
