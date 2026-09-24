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

# Copy requirements + the in-repo shared packages (ba2trade-common/-providers/-experts).
# requirements.txt deliberately does not list the packages, so they are installed explicitly
# from the build context, in the same resolve as the third-party deps. Non-editable on purpose:
# only site-packages is carried into the final stage.
COPY requirements.txt .
COPY packages/common packages/common
COPY packages/providers packages/providers
COPY packages/experts packages/experts

# Install dependencies with uv (much faster than pip). --no-sources: install exactly the copies
# above, rather than letting uv follow the packages' [tool.uv.sources] paths to each other.
RUN uv pip install --no-cache --system --no-sources \
        ./packages/common ./packages/providers "./packages/experts[ui]" \
        -r requirements.txt


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
