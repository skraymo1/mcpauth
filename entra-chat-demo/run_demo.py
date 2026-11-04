"""Run the local web/API servers, starting the mock IdP only in explicit mock mode."""
import logging
import sys
import threading
import time
import webbrowser

from werkzeug.serving import make_server

import config
import mcp_server
import webapp


def start_all():
    servers = []
    applications = [(mcp_server.app, config.MCP_PORT), (webapp.app, config.WEB_PORT)]
    if config.USE_MOCK:
        import idp_mock
        applications.insert(0, (idp_mock.app, config.IDP_PORT))
    for app, port in applications:
        server = make_server(config.HOST, port, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    return servers


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-4s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    servers = start_all()
    print(f"Auth      {config.AUTH_MODE}: {config.ISSUER}\nMCP       {config.MCP_URL}\n"
          f"Chat      {webapp.ENGINE}\nWeb app   {config.WEB_URL}\nCtrl+C to stop.", flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(config.WEB_URL)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for server in servers:
            server.shutdown()
