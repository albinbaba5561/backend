# Use Python 3.11 slim image
FROM python:3.11-slim-bullseye

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first (for caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create startup script
RUN echo '#!/bin/bash\n\
echo "Starting FastAPI server on port $PORT..."\n\
exec python main.py\n\
' > /app/start.sh && chmod +x /app/start.sh

# Expose port (Railway assigns PORT env var)
EXPOSE $PORT

# Run the application
CMD ["/app/start.sh"]