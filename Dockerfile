# Official slim Python image: small, with no compilers or docs we don't need at runtime.
FROM python:3.12-slim

# Unbuffered output so `docker logs` shows prints immediately; skip .pyc files and pip's cache to keep layers small.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Every later relative path (COPY, RUN, CMD) resolves against /app.
WORKDIR /app

# Create the unprivileged user that runs the app, plus the only dirs it may write to:
# .agentflow/ (session data, mount a volume here) and logs/ (LLM prompt debug logs).
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/.agentflow /app/logs \
    && chown app:app /app/.agentflow /app/logs

# Copy only the dependency list first, so the slow pip layer below is reused until requirements.txt changes.
COPY requirements.txt .

# Install Python dependencies (cached as long as requirements.txt is unchanged).
RUN pip install -r requirements.txt

# Everything from here on (RUN and the final CMD) runs as the non-root user.
USER app

# Pre-fetch Chroma's default embedding model into ~app/.cache/chroma, so containers don't download ~80 MB on first use.
# Chroma only checks the extracted files, so the archive is deleted in the same layer to save ~80 MB.
RUN python -c "from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2 as E; E()(['warmup'])" \
    && rm ~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx.tar.gz

# Copy the source last: it changes most often, so only this layer is rebuilt on code edits.
# Files stay root-owned, so the app can read but not modify its own code. config.yaml comes along here.
COPY . .

# Document the port uvicorn listens on (publishing still needs `-p`).
EXPOSE 8000

# Start the SSE server, listening on all interfaces so it is reachable from outside the container.
CMD ["uvicorn", "src.sse_server.app:app", "--host", "0.0.0.0", "--port", "8000"]
