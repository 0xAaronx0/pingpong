FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# SQLite lives on a mounted volume so it survives redeploys.
ENV PINGPONG_DB=/data/pingpong.db
EXPOSE 8000

# Single worker on purpose: the anti-replay nonce store and rate limiter are
# in-memory (see app.py). Scaling out needs them moved to SQLite/Redis first.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
