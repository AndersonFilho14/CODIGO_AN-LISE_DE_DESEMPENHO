import json
import math
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def count_primes(limit: int) -> int:
    """
    Carga intensiva de CPU (CPU-bound):
    Verifica numeros primos ate N por divisao direta O(N * sqrt(N))
    exatamente com a mesma logica de Rust e Java.
    """
    count = 0
    for num in range(2, limit + 1):
        is_prime = True
        sqrt = int(math.isqrt(num))
        for div in range(2, sqrt + 1):
            if num % div == 0:
                is_prime = False
                break
        if is_prime:
            count += 1
    return count


# --- Aplicacao ASGI (para rodar com Uvicorn / Gunicorn em multiplos processos) ---
async def app(scope, receive, send):
    if scope["type"] != "http":
        return

    path = scope.get("path", "")
    method = scope.get("method", "GET")

    if method != "GET":
        await send({"type": "http.response.start", "status": 405, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        return

    if path == "/health":
        body = json.dumps({"status": "ok", "lang": "python"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})
        return

    if path == "/compute":
        query_string = scope.get("query_string", b"").decode("utf-8")
        params = parse_qs(query_string)
        try:
            n = int(params.get("n", ["50000"])[0])
        except ValueError:
            n = 50000

        start = time.perf_counter()
        primes = count_primes(n)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        body = json.dumps({
            "lang": "python",
            "n": n,
            "primes": primes,
            "elapsed_ms": round(elapsed_ms, 3),
        }).encode("utf-8")

        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})
        return

    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b"Not Found"})


# --- Fallback nativo (sem dependencias externas: python3 server.py) ---
class BenchmarkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            payload = {"status": "ok", "lang": "python"}
            self._send_json(200, payload)
        elif parsed.path == "/compute":
            params = parse_qs(parsed.query)
            try:
                n = int(params.get("n", ["50000"])[0])
            except ValueError:
                n = 50000

            start = time.perf_counter()
            primes = count_primes(n)
            elapsed_ms = (time.perf_counter() - start) * 1000.0

            payload = {
                "lang": "python",
                "n": n,
                "primes": primes,
                "elapsed_ms": round(elapsed_ms, 3),
            }
            self._send_json(200, payload)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Desativa log a cada requisicao para nao criar gargalo de I/O no terminal durante o JMeter
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), BenchmarkHandler)
    print(f"[Python] Servidor nativo (ThreadingHTTPServer) rodando em http://0.0.0.0:{port}")
    server.serve_forever()
