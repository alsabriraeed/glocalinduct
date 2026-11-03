FROM python:3.11-slim

WORKDIR /app

# Install system utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy package definitions and source for editable install
COPY pyproject.toml README.md ./
COPY src/ ./src/

# CUDA torch first (GPU embeddings; the host driver libs and device nodes are passed in by docker-compose.yml)
RUN pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cu126
RUN pip install --no-cache-dir -e ".[dev,anthropic,semantic]"

# Copy application directories
COPY configs/ ./configs/
COPY prompts/ ./prompts/
COPY schemas/ ./schemas/
COPY storage/ ./storage/
COPY data/ ./data/
COPY src/ ./src/

# Expose web service port
EXPOSE 8000

ENV PYTHONUNBUFFERED=1
# Hugging Face model download over plain HTTPS (the xet backend stalled in this environment)
ENV HF_HUB_DISABLE_XET=1

# Include the Ollama CLI inside the application container
COPY --from=ollama/ollama:latest /bin/ollama /usr/local/bin/ollama

# Default command: launch the web server
CMD ["glocalinduct", "serve", "--host", "0.0.0.0", "--port", "8000"]
