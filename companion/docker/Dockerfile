# Plate Gate Companion for Frigate-in-Docker users. Build context = the companion/ folder.
# Debian-based (glibc) because onnxruntime publishes no Alpine wheels.
FROM python:3.11-slim-bookworm
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY app/plate_gate_companion /app/plate_gate_companion
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PG_DEPLOYMENT=docker PG_FRIGATE_CONFIG_DIR=/frigate_config PG_FRIGATE_MEDIA_DIR=/media/frigate
CMD ["python3", "-m", "plate_gate_companion"]
