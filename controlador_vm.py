#!/usr/bin/env python3
"""
Controlador leve que roda na VM (porta 9000).
Recebe da maquina principal:
  GET /start?lang=rust&ram_mb=512&cores=1
  1. Desativa imediatamente qualquer outra linguagem que esteja aberta
  2. Aplica os limites de CPU (taskset + cgroups) e Memoria RAM (cgroups MemoryMax + JVM -Xmx)
  3. Inicia apenas a linguagem solicitada:
     - Rust: Axum + Tokio
     - Java: Spring Boot 3 (Tomcat Embedded + Virtual Threads)
     - Python: FastAPI + Uvicorn
  GET /stop
  4. Desativa a linguagem ao final do teste
"""

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
current_proc = None
current_lang = None


def kill_all_servers():
    """Garante que nenhum servidor Python, Java ou Rust fique aberto na porta 8080."""
    global current_proc, current_lang
    if current_proc is not None:
        try:
            os.killpg(os.getpgid(current_proc.pid), signal.SIGTERM)
            current_proc.wait(timeout=3)
        except Exception:
            try:
                os.killpg(os.getpgid(current_proc.pid), signal.SIGKILL)
            except Exception:
                pass
        current_proc = None
        current_lang = None

    subprocess.run("pkill -f 'python3.*server.py' || true", shell=True)
    subprocess.run("pkill -f 'uvicorn.*server:app' || true", shell=True)
    subprocess.run("pkill -f 'benchmark-java.jar' || true", shell=True)
    subprocess.run("pkill -f 'java.*Server' || true", shell=True)
    subprocess.run("pkill -f 'benchmark-rust' || true", shell=True)
    time.sleep(0.5)


def start_language(lang: str, ram_mb: int, cores: int) -> tuple[bool, str]:
    global current_proc, current_lang
    kill_all_servers()

    cores = max(1, cores)
    ram_mb = max(128, ram_mb)

    env = os.environ.copy()
    env["PORT"] = "8080"
    env["LIMIT_CORES"] = str(cores)
    env["LIMIT_RAM_MB"] = str(ram_mb)
    env["TOKIO_WORKER_THREADS"] = str(cores)
    env["THREADS"] = str(max(4, cores * 4))

    if lang == "python":
        workdir = os.path.join(BASE_DIR, "python")
        venv_uvicorn = os.path.join(workdir, "venv", "bin", "uvicorn")
        if os.path.exists(venv_uvicorn):
            uvicorn_cmd = [venv_uvicorn]
        elif shutil.which("uvicorn"):
            uvicorn_cmd = ["uvicorn"]
        else:
            uvicorn_cmd = ["python3", "-m", "uvicorn"]

        base_cmd = uvicorn_cmd + [
            "server:app",
            "--host", "0.0.0.0",
            "--port", "8080",
            "--workers", str(cores),
            "--log-level", "warning",
            "--no-access-log",
        ]
    elif lang == "java":
        workdir = os.path.join(BASE_DIR, "java")
        jar_file = os.path.join(workdir, "target", "benchmark-java.jar")
        if not os.path.exists(jar_file):
            res = subprocess.run(
                ["mvn", "clean", "package", "-DskipTests"],
                cwd=workdir,
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                return False, f"Erro ao compilar Java (Spring Boot): {res.stderr}"
        heap_mb = max(64, int(ram_mb * 0.80))
        base_cmd = ["java", "-Xms64m", f"-Xmx{heap_mb}m", "-jar", jar_file]
    elif lang == "rust":
        workdir = os.path.join(BASE_DIR, "rust")
        bin_path = os.path.join(workdir, "target", "release", "benchmark-rust")
        if not os.path.exists(bin_path):
            cargo_bin = os.path.expanduser("~/.cargo/bin/cargo")
            cargo_cmd = cargo_bin if os.path.exists(cargo_bin) else "cargo"
            res = subprocess.run([cargo_cmd, "build", "--release"], cwd=workdir, capture_output=True, text=True)
            if res.returncode != 0:
                return False, f"Erro ao compilar Rust: {res.stderr}"
        base_cmd = [bin_path]
    else:
        return False, f"Linguagem desconhecida: {lang}"

    # Fixa afinidade de nucleos de CPU via taskset (ex: 0 para 1 core, 0-1 para 2 cores)
    cpu_range = "0" if cores == 1 else f"0-{cores - 1}"
    if shutil.which("taskset"):
        cmd = ["taskset", "-c", cpu_range] + base_cmd
    else:
        cmd = base_cmd

    # Se estiver rodando como root (via systemd service), aplica tambem limite estrito de RAM no cgroup v2
    if os.geteuid() == 0 and shutil.which("systemd-run"):
        cmd = [
            "systemd-run",
            "--scope",
            "--quiet",
            "-p", f"MemoryMax={ram_mb}M",
            "-p", "MemorySwapMax=0",
            "-p", f"AllowedCPUs={cpu_range}",
        ] + cmd

    current_proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    current_lang = lang

    for _ in range(45):
        if current_proc.poll() is not None:
            return False, f"O processo de {lang} encerrou inesperadamente."
        time.sleep(0.2)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1) as r:
                if r.status == 200:
                    return (
                        True,
                        f"Servidor {lang.upper()} ativo (PID {current_proc.pid} | Limite: {ram_mb}MB RAM, {cores} Core(s)).",
                    )
        except Exception:
            pass

    return False, f"Timeout aguardando {lang} iniciar na porta 8080."


class ControllerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/status":
            self._send_json(200, {
                "status": "controller_ok",
                "active_lang": current_lang,
            })

        elif parsed.path == "/start":
            lang = params.get("lang", [""])[0].lower()
            ram_mb = int(params.get("ram_mb", ["1024"])[0])
            cores = int(params.get("cores", ["1"])[0])
            ok, msg = start_language(lang, ram_mb, cores)
            self._send_json(200 if ok else 500, {
                "ok": ok,
                "lang": lang,
                "ram_mb": ram_mb,
                "cores": cores,
                "message": msg,
            })

        elif parsed.path == "/stop":
            kill_all_servers()
            self._send_json(200, {
                "ok": True,
                "message": "Servidor encerrado e memoria liberada na VM.",
            })

        else:
            self._send_json(404, {"error": "Not found"})

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("CONTROLLER_PORT", "9000"))
    server = HTTPServer(("0.0.0.0", port), ControllerHandler)
    print(f"[Controlador VM] Escutando na porta {port} (gerencia Python/Uvicorn, Java/Spring e Rust/Axum)")
    server.serve_forever()
