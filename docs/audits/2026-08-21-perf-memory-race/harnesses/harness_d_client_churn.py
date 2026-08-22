"""Harness D — a fresh `anthropic.Anthropic()` (and thus a fresh httpx
connection pool) is constructed for EVERY LLM call.

`get_anthropic_client()` is called inside `generate_agent_response`,
`generate_with_tools` and `synthesize_profile` on every invocation
(src/services/llm.py). Each `Anthropic()` owns its own httpx.Client, so no
TCP/TLS connection is ever reused across turns: every turn (and every
specialist consult, memory update, retry burst boundary) pays a new TCP +
TLS handshake to api.anthropic.com.

Verified against the REAL llm.py call path with the SDK pointed at a local
HTTP server that counts inbound TCP connections. Control: one shared client
issuing the same number of requests reuses a single connection.
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["ANTHROPIC_API_KEY"] = "fake-key-for-audit"
os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8917"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

CONNECTIONS = []
REQUESTS = [0]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        CONNECTIONS.append(self.client_address)
        super().setup()

    def do_POST(self):
        REQUESTS[0] += 1
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({
            "id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 8917), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

from src.services import llm  # noqa: E402


async def main():
    n = 8

    # --- Production path: every generate_agent_response call builds a client
    CONNECTIONS.clear(); REQUESTS[0] = 0
    t0 = time.perf_counter()
    for i in range(n):
        await llm.generate_agent_response(
            system_prompt="s", messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-5", max_tokens=64,
        )
    prod_time = time.perf_counter() - t0
    prod_conns = len(CONNECTIONS)

    # --- Control: one client reused for the same n calls (what a module-level
    # client would do), driven through the same _acreate seam.
    CONNECTIONS.clear(); REQUESTS[0] = 0
    shared = llm.get_anthropic_client()
    t0 = time.perf_counter()
    for i in range(n):
        await llm._acreate(
            shared, model="claude-sonnet-5", max_tokens=64, system="s",
            messages=[{"role": "user", "content": "hi"}],
        )
    ctrl_time = time.perf_counter() - t0
    ctrl_conns = len(CONNECTIONS)

    # --- Client construction cost alone
    t0 = time.perf_counter()
    for _ in range(50):
        llm.get_anthropic_client()
    per_client_ms = (time.perf_counter() - t0) / 50 * 1000

    print(f"{n} calls through the PRODUCTION path (fresh client per call): "
          f"{prod_conns} TCP connections, {prod_time*1000:.0f} ms total")
    print(f"{n} calls through ONE shared client:                        "
          f"{ctrl_conns} TCP connection(s), {ctrl_time*1000:.0f} ms total")
    print(f"Anthropic() construction alone: {per_client_ms:.1f} ms per client")
    print("\nAgainst the real API each new connection additionally pays a full "
          "TCP+TLS handshake (~50-200 ms RTT-dependent), on every LLM call in "
          "the engine: thread replies, consults (up to 8/turn), memory updates.")


asyncio.run(main())
server.shutdown()
