# Use Python 3.12 slim image
FROM python:3.12-slim

# Install ffmpeg for voice functionality
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .

# Expose port for health checks (Render uses PORT env var)
EXPOSE 8080

# Run the bot
CMD ["python", "-u", "main.py"]
