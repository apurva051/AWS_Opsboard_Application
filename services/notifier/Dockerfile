FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY worker.py .

RUN useradd --system --uid 1001 appuser
USER appuser

# No port exposed: this service pulls work from a queue instead of receiving HTTP requests.
CMD ["python", "worker.py"]
