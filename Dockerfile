FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Node 22 for Bitget grid worker (same container as Protocol FastAPI)
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node -v && npm -v

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
RUN chmod +x scripts/start_railway.sh \
    && test -f vendor/bitget-grid/server.js

EXPOSE 8001

# Public $PORT = Protocol FastAPI; Worker binds internal 8080; /grid-bot proxies to it.
CMD ["bash", "scripts/start_railway.sh"]
