# Use a slim image with Python 3.12
FROM python:3.12-slim

# Install ffmpeg for voice playback
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set workdir
WORKDIR /app

# Copy deps and install
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . /app

# Entrypoint
CMD ["python", "main.py"]
