"""Container entrypoint: one process serving the web app, with the MCP resource server on loopback only.

The web app keeps sessions in memory, so run exactly one process and one replica.
"""
import logging
import signal
import threading

from waitress import serve
from waitress.server import create_server

import config
import mcp_server
import webapp

THREADS = 8


def start_mcp():
    """Serve the MCP resource server on loopback; only this container's web app can reach it."""
    server = create_server(mcp_server.app, host=config.HOST, port=config.MCP_PORT, threads=THREADS, ident="mcp")
    threading.Thread(target=server.run, daemon=True, name="mcp").start()
    return server


def terminate(*_):
    raise SystemExit(0)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-4s %(message)s", datefmt="%H:%M:%S")
    # PID 1 ignores SIGTERM unless a handler is installed, which would delay every stop and restart.
    signal.signal(signal.SIGTERM, terminate)
    mcp = start_mcp()
    logging.getLogger("serve").info("auth=%s chat=%s web=%s:%s mcp=%s:%s", config.AUTH_MODE, webapp.ENGINE,
                                    config.WEB_BIND_HOST, config.WEB_PORT, config.HOST, config.MCP_PORT)
    try:
        serve(webapp.app, host=config.WEB_BIND_HOST, port=config.WEB_PORT, threads=THREADS, ident="web")
    finally:
        mcp.close()


if __name__ == "__main__":
    main()
