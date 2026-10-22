FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stockbots ./stockbots

# Run as a normal user; /data holds settings (keys) and the database.
RUN useradd --create-home --uid 10001 bots \
 && mkdir -p /data \
 && chown bots:bots /data
USER bots
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "stockbots"]
