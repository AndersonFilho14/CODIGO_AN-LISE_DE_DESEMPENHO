from contextlib import asynccontextmanager
import math
import os
import threading
import time

from fastapi import FastAPI, Query
import psutil
import uvicorn

# --- Limites de Hardware Recebidos pelo Controlador ---
TOTAL_VCPUS = psutil.cpu_count(logical=True) or 1
LIMIT_CORES = int(os.environ.get("LIMIT_CORES", str(TOTAL_VCPUS)))
LIMIT_RAM_MB = float(os.environ.get("LIMIT_RAM_MB", "0"))

# --- Estado Global de Monitoramento (psutil) ---
metrics_lock = threading.Lock()
start_time = time.time()
total_requests = 0
history_samples: list[dict] = []
current_process = psutil.Process(os.getpid())


def reset_metrics() -> None:
    global start_time, total_requests, history_samples
    with metrics_lock:
        start_time = time.time()
        total_requests = 0
        history_samples = []


def monitor_loop() -> None:
    """
    Thread em background que coleta o uso de CPU e Memoria a cada 1 segundo
    do inicio ate o fim da execucao, considerando os Cores e RAM alocados.
    """
    psutil.cpu_percent(interval=None, percpu=True)
    while True:
        time.sleep(1.0)
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        active_cpus = per_cpu[: max(1, min(LIMIT_CORES, len(per_cpu)))]
        cpu_pct = sum(active_cpus) / len(active_cpus) if active_cpus else 0.0

        vm = psutil.virtual_memory()
        proc_mem_mb = current_process.memory_info().rss / (1024.0 * 1024.0)
        vm_used_mb = (vm.total - vm.available) / (1024.0 * 1024.0)

        if LIMIT_RAM_MB > 0:
            mem_used_mb = proc_mem_mb
            mem_pct = min(100.0, (proc_mem_mb / LIMIT_RAM_MB) * 100.0)
        else:
            mem_used_mb = vm_used_mb
            mem_pct = vm.percent

        with metrics_lock:
            elapsed = round(time.time() - start_time, 2)
            history_samples.append({
                "elapsed_sec": elapsed,
                "cpu_percent": round(cpu_pct, 2),
                "mem_percent": round(mem_pct, 2),
                "mem_used_mb": round(mem_used_mb, 2),
                "vm_total_used_mb": round(vm_used_mb, 2),
                "process_mem_mb": round(proc_mem_mb, 2),
                "requests_so_far": total_requests,
            })


@asynccontextmanager
async def lifespan(_app: FastAPI):
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    yield


app = FastAPI(
    title="Benchmark Python API (FastAPI + Uvicorn)",
    lifespan=lifespan,
)


def count_primes(limit: int) -> int:
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


@app.get("/health")
def health_endpoint() -> dict:
    return {
        "status": "ok",
        "lang": "python",
        "cores": LIMIT_CORES,
        "ram_mb": LIMIT_RAM_MB,
    }


@app.get("/metrics/reset")
def reset_metrics_endpoint() -> dict:
    reset_metrics()
    return {"status": "metrics_reset", "lang": "python"}


@app.get("/metrics")
def metrics_endpoint() -> dict:
    vm = psutil.virtual_memory()
    mem_total_mb = round(
        LIMIT_RAM_MB if LIMIT_RAM_MB > 0 else (vm.total / (1024.0 * 1024.0)), 2
    )

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
        "limits": {
            "cores": LIMIT_CORES,
            "ram_mb": mem_total_mb,
        },
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


@app.get("/compute")
def compute_endpoint(n: int = Query(default=50000, ge=2)) -> dict:
    """
    Endpoint sincrono no FastAPI: executado automaticamente no threadpool do AnyIO/Starlette
    para nao bloquear o event loop do Uvicorn durante o calculo CPU-bound.
    """
    global total_requests
    start = time.perf_counter()
    primes = count_primes(n)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    with metrics_lock:
        total_requests += 1

    return {
        "lang": "python",
        "n": n,
        "primes": primes,
        "elapsed_ms": round(elapsed_ms, 3),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(
        f"[Python/FastAPI+Uvicorn] Servidor rodando em http://0.0.0.0:{port} "
        f"(Cores={LIMIT_CORES}, RAM={LIMIT_RAM_MB}MB)"
    )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
