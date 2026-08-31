FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY *.py ./
COPY config.yaml .

# Persistent data lives in /data
VOLUME ["/data"]

CMD ["python3", "main.py"]
