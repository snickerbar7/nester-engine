# Harriet Nester service — Python 3.13 + tube/sheet engines + OpenCASCADE.
FROM python:3.13-slim

# Runtime libs OpenCASCADE (cadquery-ocp) needs; shapely/spyrrow ship wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglu1-mesa libxrender1 libxext6 libsm6 libx11-6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt requirements-service.txt ./
RUN pip install --no-cache-dir -r requirements-service.txt

# App source (venvs/output are excluded via .dockerignore).
COPY nester ./nester
COPY service ./service
COPY solid_nest.py ./solid_nest.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Render provides $PORT; default 8000 for local runs.
CMD ["sh", "-c", "uvicorn service.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
