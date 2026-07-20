FROM python:3.12-slim

# Slimmed install: writer + core only, no Impress/Calc/Draw.
# --no-install-recommends keeps out the recommended-but-unneeded extras
# (help packs, extra filters, etc.) that bloat the image further.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    libreoffice-core \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/lib/libreoffice/help \
    && rm -rf /usr/share/libreoffice/help

RUN pip install --no-cache-dir flask gunicorn

RUN useradd -m -u 1000 -s /usr/sbin/nologin converter

WORKDIR /app
COPY app.py .

RUN mkdir -p /tmp/conversions && chown -R converter:converter /app /tmp/conversions

USER converter

EXPOSE 8000
# Single worker: our own semaphore manages soffice concurrency internally. Maybe this is a bad choice, IDK
# multiple processes of soffice don't seem to be safely concurrent. Hence this choice
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8000", "--timeout", "150", "app:app"]
