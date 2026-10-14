# ProdSentinel - AI production disruption early warning
# Build:  docker build -t prodsentinel .
# Run:    docker run -p 8000:8000 prodsentinel      -> http://localhost:8000
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 \
    EWS_PACK=industries/beverage_bottling.yaml EWS_OPEN_DEMO=1 PORT=8000
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine engine
COPY api api
COPY industries industries
COPY web web
COPY data/raw data/raw
COPY run.py .

# Build the plant data, features, model and evaluation report into the image (~3 min).
RUN python run.py all

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health')"
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
