"""Docker healthcheck: GET /healthz over the app's Unix socket."""
import socket
import sys

try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect("/run/sock/app.sock")
    s.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
    ok = s.recv(64).split(b"\r\n", 1)[0].split(b" ")[1:2] == [b"200"]
except OSError:
    ok = False
sys.exit(0 if ok else 1)
