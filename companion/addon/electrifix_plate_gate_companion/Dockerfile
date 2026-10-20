# ElectriFix Plate Gate Companion: Home Assistant's Debian base + Python 3.11 in a venv + pinned,
# hash-verified wheels (paho-mqtt, onnxruntime, numpy, Pillow and their dependencies) + our package.
# Nothing else is installed. Debian rather than Alpine because onnxruntime has no musl wheels.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY app/plate_gate_companion /app/plate_gate_companion
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
