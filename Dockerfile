FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Node for vendor/wangge (3xx-wangge absolute — Decibel/Extended/RISEx + AI UI)
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node -v && npm -v

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

WORKDIR /app/vendor/wangge
RUN test -f src/server.js \
    && npm install --omit=dev \
    && chmod +x /app/scripts/start_railway.sh

WORKDIR /app

EXPOSE 8001

# Public $PORT = Protocol; wangge binds 127.0.0.1:8080; middleware proxies dashboard/API.
CMD ["bash", "scripts/start_railway.sh"]
