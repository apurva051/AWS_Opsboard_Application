FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .

# Run as a non-root user.
RUN useradd --system --uid 1001 appuser
USER appuser

EXPOSE 8080
# --preload runs init_db() once before the workers fork.
CMD ["gunicorn", "--preload", "--workers", "2", "--bind", "0.0.0.0:8080", "--access-logfile", "-", "app:app"]
