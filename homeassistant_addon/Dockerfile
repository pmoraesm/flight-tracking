FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY *.py ./
COPY run.sh .
RUN chmod +x run.sh

# Persistent data lives in /data (mounted by HA)
VOLUME ["/data"]

ENV FLIGHT_DB_PATH=/data/prices.db

CMD ["/app/run.sh"]
