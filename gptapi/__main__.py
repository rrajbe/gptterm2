import logging
import os
import sys

from aiohttp import web

from .config import Config
from .server import build_app


def main():
    cfg = Config.from_env()
    os.makedirs(cfg.data, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO), format=fmt,
                        handlers=[logging.StreamHandler(sys.stderr),
                                  logging.FileHandler(os.path.join(cfg.data, "gptapi.log"))])
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    log = logging.getLogger("gptapi")
    log.info("gptapi on http://%s:%s  cdp=%s  launch_browser=%s  data=%s  max_tabs=%s  auth=%s",
             cfg.host, cfg.port, cfg.cdp_url, cfg.launch_browser, cfg.data, cfg.max_tabs, bool(cfg.api_key))
    web.run_app(build_app(cfg), host=cfg.host, port=cfg.port, print=None, shutdown_timeout=35)


if __name__ == "__main__":
    main()
