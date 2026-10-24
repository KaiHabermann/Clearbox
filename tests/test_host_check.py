"""Host and Origin checks, against a running --mock server on 127.0.0.1.

A web page must not drive the local server: DNS rebinding (the page's own name resolving to
127.0.0.1) shows up as a foreign Host header and gets a 421 on REST, /mcp and /mcp-agent alike;
another page calling across origins shows up as a foreign Origin and gets a 403. The server's
own names pass, with or without a same-origin Origin, and the endless /api/stream still streams.
Raw sockets, because urllib would fill in Host itself.

Run:  uv run clearbox --mock      (one shell)
      uv run python tests/test_host_check.py [http://127.0.0.1:<port>]  (default: $CLEARBOX_PORT, else 8765)
"""
import json, os, socket, sys
from urllib.parse import urlsplit

URL = (sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{os.environ.get('CLEARBOX_PORT', '8765')}").rstrip("/")
PORT = urlsplit(URL).port

MCP_INIT = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})


def send(method, path, host, origin=None, body=None, read=4096):
    """Status code and the first bytes of the response."""
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close",
             "Accept: application/json, text/event-stream"]
    if origin:
        lines.append(f"Origin: {origin}")
    data = (body or "").encode()
    if body is not None:
        lines += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
        got = b""
        while len(got) < read:
            chunk = s.recv(read)
            if not chunk:
                break
            got += chunk
            if method == "GET" and path == "/api/stream" and b"event: hello" in got:
                break
    head = got.decode("latin-1")
    return int(head.split(" ", 2)[1]), head


def main():
    ours, evil = f"127.0.0.1:{PORT}", f"evil.example:{PORT}"

    # a foreign Host is refused everywhere, before any route runs
    for method, path, body in [("GET", "/api/plans", None), ("POST", "/api/agents", "{}"),
                               ("GET", "/", None), ("POST", "/mcp", MCP_INIT),
                               ("POST", "/mcp-agent/x", MCP_INIT)]:
        code, text = send(method, path, evil, body=body)
        assert code == 421, (method, path, code, text)
        assert "not allowed" in text, text
    print("ok  foreign Host refused on /api, /, /mcp and /mcp-agent")

    # the server's own names pass
    for host in (ours, f"localhost:{PORT}", "localhost"):
        code, text = send("GET", "/api/plans", host)
        assert code == 200, (host, code, text)
    code, text = send("POST", "/mcp", ours, body=MCP_INIT)
    assert code == 200, (code, text)
    code, _ = send("POST", "/mcp-agent/x", ours, body=MCP_INIT)
    assert code == 404, code  # passes the check, then the unknown token is turned away
    print("ok  127.0.0.1 and localhost pass on /api and /mcp")

    # Origin: a foreign page is refused, the UI's own requests and Origin-less clients are not
    for origin in ("http://evil.example", f"http://localhost:{PORT + 1}", "null"):
        code, text = send("POST", "/api/agents", ours, origin=origin, body="{}")
        assert code == 403, (origin, code, text)
        code, _ = send("POST", "/mcp", ours, origin=origin, body=MCP_INIT)
        assert code == 403, (origin, code)
    for origin in (f"http://{ours}", f"http://localhost:{PORT}", None):
        code, text = send("POST", "/api/agents", ours, origin=origin, body="{}")
        assert code == 422, (origin, code, text)  # reached the route: the empty body is invalid
    print("ok  foreign Origin refused, same-origin and no Origin pass")

    code, text = send("GET", "/api/stream", ours, origin=f"http://{ours}")
    assert code == 200 and "text/event-stream" in text and "event: hello" in text, (code, text)
    print("ok  /api/stream still streams")
    print("ALL OK")


if __name__ == "__main__":
    main()
