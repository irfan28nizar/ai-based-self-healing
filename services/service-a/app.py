import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SERVICE_NAME = os.environ.get("SERVICE_NAME", "service-a")
PORT = int(os.environ.get("PORT", "8000"))


class ServiceHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404, "Not Found")
            return

        body = json.dumps(
            {"service": SERVICE_NAME, "phase": 1, "message": "service is running"}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ServiceHandler)
    print(f"{SERVICE_NAME} listening on port {PORT}")
    server.serve_forever()
