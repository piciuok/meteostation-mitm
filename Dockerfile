FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    HTTP_PORT=80

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY weatherbridge/ weatherbridge/
COPY weatherstation.py .

VOLUME ["/data"]
EXPOSE 80 6053

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('HTTP_PORT','80'), timeout=4)" || exit 1

CMD ["python", "weatherstation.py"]
