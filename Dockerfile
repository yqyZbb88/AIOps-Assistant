FROM python:3.13.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt
COPY . .
RUN mkdir -p /app/data /app/logs

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=6s --start-period=20s --retries=4 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)"
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
