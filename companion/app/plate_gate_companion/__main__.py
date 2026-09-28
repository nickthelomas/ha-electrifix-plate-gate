"""python -m plate_gate_companion"""
import logging

from .service import Companion, Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
cfg = Config.from_env()
logging.getLogger(__name__).info(
    "Plate Gate Companion %s starting: id=%s deployment=%s config_dir=%s mqtt=%s:%s",
    __import__("plate_gate_companion").VERSION, cfg.companion_id, cfg.deployment, cfg.config_dir, cfg.mqtt_host, cfg.mqtt_port,
)
Companion(cfg).run_forever()
