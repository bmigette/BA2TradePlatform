# BA2 Trade Platform Docker Image
# Multi-stage build with uv for optimization
#
# Why multi-stage build?
# - Stage 1 (builder): Installs uv and compiles dependencies (~2-3GB intermediate)
# - Stage 2 (final): Copies only site-packages + source (~600-800MB final image)
# 
# Benefits:
# - 60-70% smaller final image (builder layer discarded)
# - Faster cloud deployment (smaller push/pull)
# - Better security (no build tools in production image)
# - Cached builder stage speeds up rebuilds

FROM python:3.11-slim as builder

WORKDIR /build

# Install uv
RUN pip install --no-cache-dir uv

# Copy requirements
COPY requirements.txt .

# Install dependencies with uv (much faster than pip)
RUN uv pip install --no-cache-dir --system -r requirements.txt


FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Create non-root user for security
RUN useradd -m -u 1000 trader && \
    mkdir -p /opt/ba2_trade_platform/db && \
    mkdir -p /opt/ba2_trade_platform/cache && \
    chown -R trader:trader /opt/ba2_trade_platform

RUN mkdir logs && chown -R trader:trader logs
# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy application code
COPY --chown=trader:trader . .

# Switch to non-root user
USER trader

# Expose port for web interface
EXPOSE 8000

# Default command with volumes mounted to /opt/ba2_trade_platform
# db and cache are in separate subdirectories for independent persistence
CMD ["python", "main.py", \
     "--db-file", "/opt/ba2_trade_platform/db/db.sqlite", \
     "--cache-folder", "/opt/ba2_trade_platform/cache", \
     "--log-folder", "/opt/ba2_trade_platform/logs", \
     "--port", "8000"]
