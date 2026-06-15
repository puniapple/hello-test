FROM python:3.12-slim

# System deps for building wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (better layer caching)
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# App code
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

# Run migrations on start, then launch the bot
CMD alembic upgrade head && python -m src.main