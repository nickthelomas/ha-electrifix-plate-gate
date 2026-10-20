# Plate Gate Companion

The small service that runs next to Frigate for ElectriFix Plate Gate. See `addon/…/DOCS.md` for the
Home Assistant add-on and `docker/docker-compose.example.yml` for Frigate-in-Docker.

Tests: `./.venv/bin/pytest -q` from this folder (its own venv: `python3 -m venv .venv && .venv/bin/pip install paho-mqtt==2.1.0 pytest`).
