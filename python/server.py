import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import psutil

# --- Estado Global de Monitoramento (psutil) ---
metrics_lock = threading.Lock()
start_time = time.time()
total_requests = 0
history_samples = []
current_process = psutil.Process(os.getpid())


def reset_metrics():
    global start_time, total_requests, history_samples
    with metrics_lock:
        start_time = time.time()
        total_requests = 0
        history_samples = []


def monitor_loop():
    """
    Thread em background que coleta o uso de CPU e Memoria da VM a cada 1 segundo
    do inicio ate o fim da execucao.
    """
    # Primeira chamada ao cpu_percent inicializa o contador do psutil
    psutil.cpu_percent(interval=None)
    while True:
        time.sleep(1.0)
        cpu_pct = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        proc_mem_mb = current_process.memory_info().rss / (1024.0 * 1024.0)
        mem_used_mb = (vm.total - vm.available) / (1024.0 * 1024.0)

        with metrics_lock:
            elapsed = round(time.time() - start_time, 2)
            history_samples.append({
                "elapsed_sec": elapsed,
                "cpu_percent": round(cpu_pct, 2),
                "mem_percent": round(vm.percent, 2),
                "mem_used_mb": round(mem_used_mb, 2),
                "process_mem_mb": round(proc_mem_mb, 2),
                "requests_so_far": total_requests,
            })


# Inicia a thread de monitoramento assim que o script sobe
monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
monitor_thread.start()


def get_metrics_payload() -> dict:
    vm = psutil.virtual_memory()
    mem_total_mb = round(vm.total / (1024.0 * 1024.0), 2)

    with metrics_lock:
        duration_sec = round(time.time() - start_time, 2)
        samples = list(history_samples)
        reqs = total_requests

    if samples:
        cpu_avg = round(sum(s["cpu_percent"] for s in samples) / len(samples), 2)
        cpu_max = round(max(s["cpu_percent"] for s in samples), 2)
        mem_avg = round(sum(s["mem_percent"] for s in samples) / len(samples), 2)
        mem_max = round(max(s["mem_percent"] for s in samples), 2)
        mem_used_max = round(max(s["mem_used_mb"] for s in samples), 2)
        proc_mem_max = round(max(s["process_mem_mb"] for s in samples), 2)
    else:
        cpu_avg = cpu_max = mem_avg = mem_max = mem_used_max = proc_mem_max = 0.0

    return {
        "lang": "python",
        "summary": {
            "duration_sec": duration_sec,
            "samples_count": len(samples),
            "total_requests": reqs,
            "cpu_percent_avg": cpu_avg,
            "cpu_percent_max": cpu_max,
            "mem_percent_avg": mem_avg,
            "mem_percent_max": mem_max,
            "mem_used_mb_max": mem_used_max,
            "mem_total_mb": mem_total_mb,
            "process_mem_mb_max": proc_mem_max,
        },
        "history": samples,
    }


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


class BenchmarkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global total_requests
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._send_json(200, {"status": "ok", "lang": "python"})

        elif parsed.path == "/metrics":
            # Devolve todo o historico e resumo de CPU e Memoria do inicio ate agora
            self._send_json(200, get_metrics_payload())

        elif parsed.path == "/metrics/reset":
            # Zera o historico caso queira reiniciar a contagem antes de um novo teste
            reset_metrics()
            self._send_json(200, {"status": "metrics_reset", "lang": "python"})

        elif parsed.path == "/compute":
            params = parse_qs(parsed.query)
            try:
                n = int(params.get("n", ["50000"])[0])
            except ValueError:
                n = 50000

            start = time.perf_counter()
            primes = count_primes(n)
            elapsed_ms = (time.perf_counter() - start) * 1000.0

            with metrics_lock:
                total_requests += 1

            self._send_json(200, {
                "lang": "python",
                "n": n,
                "primes": primes,
                "elapsed_ms": round(elapsed_ms, 3),
            })
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
        # Desativa log a cada requisicao para nao criar gargalo de I/O no terminal
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), BenchmarkHandler)
    print(f"[Python] Servidor rodando em http://0.0.0.0:{port}")
    print(f"  -> Carga CPU : http://0.0.0.0:{port}/compute?n=50000")
    print(f"  -> Metricas  : http://0.0.0.0:{port}/metrics")
    print(f"  -> Resetar   : http://0.0.0.0:{port}/metrics/reset")
    server.serve_forever()
