# Harriet Nester service — Python 3.13 + tube/sheet engines.
# NOTE: OpenCASCADE (cadquery-ocp) is DEFERRED until the STEP reader lands; when
# re-added, restore the apt libs it needs: libgl1 libglu1-mesa libxrender1
# libxext6 libsm6 libx11-6. The light path (IGES tube + DXF sheet) needs none —
# reportlab/ezdxf/shapely/numpy/spyrrow ship self-contained wheels.
FROM python:3.13-slim

WORKDIR /app

# Doppler CLI — Harriet is Doppler-canonical; the container pulls NESTER_SERVICE_TOKEN
# + R2_* from the `nester` project at runtime via `doppler run` (Render supplies DOPPLER_TOKEN).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -Ls https://cli.doppler.com/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

# Install deps first for layer caching.
COPY requirements.txt requirements-service.txt ./
RUN pip install --no-cache-dir -r requirements-service.txt

# App source (venvs/output are excluded via .dockerignore).
COPY nester ./nester
COPY service ./service
COPY solid_nest.py ./solid_nest.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Render provides $PORT + DOPPLER_TOKEN; `doppler run` injects the rest from Doppler `nester/prd`.
CMD ["sh", "-c", "doppler run -- uvicorn service.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
