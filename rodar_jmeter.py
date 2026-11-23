#!/usr/bin/env python3
"""
Orquestrador Automatico de Benchmark (Rust, Java, Python):
  1. Suporta multiplas repeticoes por configuracao (padrao: 5 execucoes) para
     calcular Media e Desvio-Padrao (amostral).
  2. Padroniza a janela de medicao de CPU/RAM da VM para a janela exata de
     carga ativa (ex: 30s de requisicoes ativas), descartando os segundos
     ociosos antes do JMeter iniciar a JVM e o escoamento pos-teste.
  3. Aplica limites de RAM (cgroups MemoryMax + JVM -Xmx) e Cores (taskset).
  4. Salva tudo organizado em:
       resultados/<RAM>_<CORES>/<linguagem>/<N>_threads_<timestamp>/
"""

import csv
import json
import math
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime


def get_jmeter_cmd() -> list[str]:
    if shutil.which("jmeter"):
        return ["jmeter"]
    if shutil.which("flatpak"):
        return ["flatpak", "run", "--filesystem=host", "org.apache.jmeter"]
    raise RuntimeError("JMeter nao encontrado nem no PATH nem via Flatpak.")


def escolher_opcao(titulo: str, opcoes: list[tuple[str, str]], permitir_custom: bool = False) -> str:
    print(f"\n=== {titulo} ===")
    for idx, (valor, descricao) in enumerate(opcoes, start=1):
        print(f"  [{idx}] {descricao}")
    if permitir_custom:
        print(f"  [{len(opcoes) + 1}] Digitar outro valor manualmente")

    while True:
        escolha = input("Escolha uma opcao (numero): ").strip()
        if escolha.isdigit():
            num = int(escolha)
            if 1 <= num <= len(opcoes):
                return opcoes[num - 1][0]
            if permitir_custom and num == len(opcoes) + 1:
                custom = input("Digite o valor desejado: ").strip()
                if custom:
                    return custom
        print("Opcao invalida! Tente novamente.")


def extrair_inteiro(texto: str, padrao: int) -> int:
    nums = re.findall(r"\d+", texto)
    return int(nums[0]) if nums else padrao


def padronizar_metricas_vm(vm_data: dict, duration_target: int, ram_mb: int) -> tuple[dict, list[dict]]:
    """
    Padroniza a janela de medicao de CPU e RAM para os 'duration_target' segundos
    (ex: 30s) de carga ativa, eliminando:
      - O atraso inicial (~3s) enquanto o JMeter abre a JVM na maquina cliente (CPU = 0%)
      - A cauda de drenagem (tail drain) apos os 30s (ex: conexoes residuais no Python)
    """
    raw_history = vm_data.get("history", [])
    raw_summary = vm_data.get("summary", {})
    mem_total_mb = float(raw_summary.get("mem_total_mb") or ram_mb)

    # Filtra a partir do primeiro segundo em que houve carga real (CPU > 1% ou requisicoes > 0)
    start_idx = 0
    for i, sample in enumerate(raw_history):
        if sample.get("requests_so_far", 0) > 0 or sample.get("cpu_percent", 0.0) > 5.0:
            start_idx = i
            break

    active_slice = raw_history[start_idx : start_idx + duration_target]
    if not active_slice and raw_history:
        active_slice = raw_history[:duration_target]

    standardized_history = []
    for idx, s in enumerate(active_slice, start=1):
        standardized_history.append({
            "elapsed_sec": float(idx),
            "cpu_percent": round(float(s.get("cpu_percent", 0.0)), 2),
            "mem_percent": round(float(s.get("mem_percent", 0.0)), 2),
            "mem_used_mb": round(float(s.get("mem_used_mb", 0.0)), 2),
            "process_mem_mb": round(float(s.get("process_mem_mb", 0.0)), 2),
            "requests_so_far": int(s.get("requests_so_far", 0)),
        })

    if standardized_history:
        count = len(standardized_history)
        cpu_avg = round(sum(s["cpu_percent"] for s in standardized_history) / count, 2)
        cpu_max = round(max(s["cpu_percent"] for s in standardized_history), 2)
        mem_avg = round(sum(s["mem_percent"] for s in standardized_history) / count, 2)
        mem_max = round(max(s["mem_percent"] for s in standardized_history), 2)
        mem_used_max = round(max(s["mem_used_mb"] for s in standardized_history), 2)
        proc_mem_max = round(max(s["process_mem_mb"] for s in standardized_history), 2)
        total_reqs = max(
            int(raw_summary.get("total_requests", 0)),
            standardized_history[-1]["requests_so_far"],
        )
    else:
        count = 0
        cpu_avg = cpu_max = mem_avg = mem_max = mem_used_max = proc_mem_max = 0.0
        total_reqs = int(raw_summary.get("total_requests", 0))

    summary = {
        "duration_sec": float(count),
        "raw_duration_sec": round(float(raw_summary.get("duration_sec", count)), 2),
        "samples_count": count,
        "total_requests": total_reqs,
        "cpu_percent_avg": cpu_avg,
        "cpu_percent_max": cpu_max,
        "mem_percent_avg": mem_avg,
        "mem_percent_max": mem_max,
        "mem_used_mb_max": mem_used_max,
        "mem_total_mb": mem_total_mb,
        "process_mem_mb_max": proc_mem_max,
    }
    return summary, standardized_history


def ativar_linguagem_na_vm(host: str, lang: str, ram_mb: int, cores: int, vm_user: str = "user"):
    controller_url = f"http://{host}:9000/start?lang={lang}&ram_mb={ram_mb}&cores={cores}"
    with urllib.request.urlopen(controller_url, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        if data.get("ok"):
            print(f"      -> {data.get('message')}")
            return
        raise RuntimeError(data.get("message", "Falha no controlador da VM"))


def desativar_linguagens_na_vm(host: str):
    controller_url = f"http://{host}:9000/stop"
    try:
        with urllib.request.urlopen(controller_url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"      -> {data.get('message')}")
    except Exception:
        pass


def init_db(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS testes_completos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT,
            pasta_vm TEXT,
            vm_ram_cfg TEXT,
            vm_cores_cfg TEXT,
            linguagem TEXT,
            threads INTEGER,
            rampup_sec INTEGER,
            duration_cfg_sec INTEGER,
            n_primos INTEGER,
            jmeter_samples INTEGER,
            jmeter_errors INTEGER,
            jmeter_error_pct REAL,
            jmeter_throughput_rps REAL,
            jmeter_avg_ms REAL,
            jmeter_min_ms REAL,
            jmeter_p95_ms REAL,
            jmeter_max_ms REAL,
            vm_duration_sec REAL,
            vm_cpu_avg REAL,
            vm_cpu_max REAL,
            vm_mem_avg REAL,
            vm_mem_max REAL,
            vm_mem_used_mb_max REAL,
            vm_mem_total_mb REAL,
            vm_process_mem_mb_max REAL,
            caminho_pasta TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS historico_vm_segundos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teste_id INTEGER,
            elapsed_sec REAL,
            cpu_percent REAL,
            mem_percent REAL,
            mem_used_mb REAL,
            process_mem_mb REAL,
            requests_so_far INTEGER,
            FOREIGN KEY (teste_id) REFERENCES testes_completos (id)
        )
    """)
    conn.commit()


def parse_jtl(jtl_path: str) -> dict:
    elapsed_list = []
    timestamps = []
    errors = 0
    total = 0

    with open(jtl_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            el = float(row["elapsed"])
            ts = int(row["timeStamp"])
            success = row["success"].strip().lower() == "true"
            if not success:
                errors += 1
            elapsed_list.append(el)
            timestamps.append((ts, ts + int(el)))

    if total == 0:
        return {
            "samples": 0, "errors": 0, "error_pct": 0.0,
            "throughput_rps": 0.0, "avg_ms": 0.0, "min_ms": 0.0,
            "p95_ms": 0.0, "max_ms": 0.0,
        }

    elapsed_list.sort()
    min_ts = min(t[0] for t in timestamps)
    max_ts = max(t[1] for t in timestamps)
    total_time_sec = max((max_ts - min_ts) / 1000.0, 0.001)

    p95_idx = min(int(len(elapsed_list) * 0.95), len(elapsed_list) - 1)

    return {
        "samples": total,
        "errors": errors,
        "error_pct": round((errors / total) * 100.0, 2),
        "throughput_rps": round(total / total_time_sec, 2),
        "avg_ms": round(sum(elapsed_list) / total, 2),
        "min_ms": round(elapsed_list[0], 2),
        "p95_ms": round(elapsed_list[p95_idx], 2),
        "max_ms": round(elapsed_list[-1], 2),
    }


def executar_bateria_linguagem(
    base_dir: str,
    jmx_path: str,
    host: str,
    port: int,
    linguagem: str,
    vm_ram_label: str,
    vm_cores_label: str,
    ram_mb: int,
    cores: int,
    threads: int,
    rampup: int,
    duration: int,
    n_primos: int,
    rep_idx: int = 1,
    rep_total: int = 1,
) -> dict:
    pasta_vm = f"{vm_ram_label}_{vm_cores_label}"
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    pasta_execucao = os.path.join(
        base_dir,
        "resultados",
        pasta_vm,
        linguagem,
        f"{threads}_threads_{ts_str}",
    )
    os.makedirs(pasta_execucao, exist_ok=True)
    base_url = f"http://{host}:{port}"

    print("\n" + "=" * 70)
    print(f" >>> [{rep_idx}/{rep_total}] {linguagem.upper()} | VM: {pasta_vm} | {threads} Threads <<<")
    print(f"     Destino: resultados/{pasta_vm}/{linguagem}/{threads}_threads_{ts_str}/")
    print("=" * 70)

    print(f"[1/5] Ativando {linguagem.upper()} na VM ({ram_mb}MB RAM, {cores} Core(s))...")
    ativar_linguagem_na_vm(host, linguagem, ram_mb, cores)

    vm_summary = {
        "duration_sec": float(duration), "raw_duration_sec": float(duration),
        "samples_count": duration, "total_requests": 0,
        "cpu_percent_avg": 0.0, "cpu_percent_max": 0.0,
        "mem_percent_avg": 0.0, "mem_percent_max": 0.0,
        "mem_used_mb_max": 0.0, "mem_total_mb": float(ram_mb), "process_mem_mb_max": 0.0,
    }
    vm_history = []
    jtl_path = os.path.join(pasta_execucao, "jmeter_requests.jtl")

    try:
        print(f"[2/5] Zerando acompanhamento de CPU e RAM na VM ({base_url}/metrics/reset)...")
        with urllib.request.urlopen(f"{base_url}/metrics/reset", timeout=5) as resp:
            resp.read()

        cmd = get_jmeter_cmd() + [
            "-n",
            "-t", jmx_path,
            "-l", jtl_path,
            f"-Jhost={host}",
            f"-Jport={port}",
            f"-Jthreads={threads}",
            f"-Jrampup={rampup}",
            f"-Jduration={duration}",
            f"-Jn={n_primos}",
        ]

        print(f"[3/5] Rodando JMeter ({threads} threads por {duration}s contra {linguagem.upper()})...")
        subprocess.run(cmd, check=True)

        print(f"[4/5] Coletando e padronizando janela de {duration}s da VM ({base_url}/metrics)...")
        with urllib.request.urlopen(f"{base_url}/metrics", timeout=15) as resp:
            vm_raw = json.loads(resp.read().decode("utf-8"))
            vm_summary, vm_history = padronizar_metricas_vm(vm_raw, duration, ram_mb)
            vm_standardized = {
                "lang": linguagem,
                "limits": vm_raw.get("limits", {"cores": cores, "ram_mb": float(ram_mb)}),
                "summary": vm_summary,
                "history": vm_history,
            }
            vm_json_path = os.path.join(pasta_execucao, "vm_metricas_psutil.json")
            with open(vm_json_path, "w", encoding="utf-8") as f:
                json.dump(vm_standardized, f, indent=2)

    finally:
        print(f"[5/5] Apagando/inativando {linguagem.upper()} na VM...")
        desativar_linguagens_na_vm(host)

    jm_stats = parse_jtl(jtl_path)
    vm_summary["total_requests"] = jm_stats["samples"]

    resumo_consolidado = {
        "data_hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "configuracao_vm": {
            "pasta_vm": pasta_vm,
            "ram_label": vm_ram_label,
            "ram_mb": ram_mb,
            "cores_label": vm_cores_label,
            "cores": cores,
        },
        "parametros_teste": {
            "linguagem": linguagem,
            "threads": threads,
            "rampup_sec": rampup,
            "duration_sec": duration,
            "n_primos": n_primos,
        },
        "jmeter_resultados": jm_stats,
        "vm_resumo_recursos": vm_summary,
    }
    resumo_json_path = os.path.join(pasta_execucao, "resumo_execucao.json")
    with open(resumo_json_path, "w", encoding="utf-8") as f:
        json.dump(resumo_consolidado, f, indent=2)

    db_path = os.path.join(base_dir, "resultados", "resultados_vm.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO testes_completos (
            data_hora, pasta_vm, vm_ram_cfg, vm_cores_cfg, linguagem,
            threads, rampup_sec, duration_cfg_sec, n_primos,
            jmeter_samples, jmeter_errors, jmeter_error_pct, jmeter_throughput_rps,
            jmeter_avg_ms, jmeter_min_ms, jmeter_p95_ms, jmeter_max_ms,
            vm_duration_sec, vm_cpu_avg, vm_cpu_max, vm_mem_avg, vm_mem_max,
            vm_mem_used_mb_max, vm_mem_total_mb, vm_process_mem_mb_max, caminho_pasta
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        resumo_consolidado["data_hora"], pasta_vm, vm_ram_label, vm_cores_label, linguagem,
        threads, rampup, duration, n_primos,
        jm_stats["samples"], jm_stats["errors"], jm_stats["error_pct"], jm_stats["throughput_rps"],
        jm_stats["avg_ms"], jm_stats["min_ms"], jm_stats["p95_ms"], jm_stats["max_ms"],
        vm_summary["duration_sec"], vm_summary["cpu_percent_avg"], vm_summary["cpu_percent_max"],
        vm_summary["mem_percent_avg"], vm_summary["mem_percent_max"],
        vm_summary["mem_used_mb_max"], vm_summary["mem_total_mb"], vm_summary["process_mem_mb_max"],
        pasta_execucao,
    ))
    teste_id = cur.lastrowid

    if vm_history:
        cur.executemany("""
            INSERT INTO historico_vm_segundos (
                teste_id, elapsed_sec, cpu_percent, mem_percent,
                mem_used_mb, process_mem_mb, requests_so_far
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            (
                teste_id,
                item["elapsed_sec"],
                item["cpu_percent"],
                item["mem_percent"],
                item["mem_used_mb"],
                item["process_mem_mb"],
                item["requests_so_far"],
            )
            for item in vm_history
        ])

    conn.commit()
    conn.close()

    return {
        "linguagem": linguagem,
        "jm": jm_stats,
        "vm": vm_summary,
        "pasta": pasta_execucao,
    }


def main():
    print("=" * 65)
    print("   ORQUESTRADOR AUTOMATICO DE BENCHMARK (Rust, Java, Python)")
    print("=" * 65)

    modo_lang = escolher_opcao(
        "1. Qual LINGUAGEM voce deseja testar?",
        [
            ("todas", "TODAS as 3 em sequencia automatica (Rust -> apaga -> Java -> apaga -> Python -> apaga)"),
            ("rust", "Apenas Rust"),
            ("java", "Apenas Java"),
            ("python", "Apenas Python"),
        ],
    )
    linguagens_para_rodar = ["rust", "java", "python"] if modo_lang == "todas" else [modo_lang]

    vm_ram_label = escolher_opcao(
        "2. Qual a MEMORIA RAM que sera disponibilizada na VM?",
        [
            ("512MB", "512 MB"),
            ("1024MB", "1.024 MB (1 GB)"),
            ("2048MB", "2.048 MB (2 GB)"),
        ],
        permitir_custom=True,
    ).replace(" ", "")
    ram_mb = extrair_inteiro(vm_ram_label, 1024)
    if not vm_ram_label.upper().endswith("MB"):
        vm_ram_label = f"{ram_mb}MB"

    vm_cores_label = escolher_opcao(
        "3. Quantos CORES (vCPUs) serao utilizados na VM?",
        [
            ("1_core", "1 Core (1 vCPU)"),
            ("2_cores", "2 Cores (2 vCPUs)"),
            ("4_cores", "4 Cores (4 vCPUs)"),
        ],
        permitir_custom=True,
    ).replace(" ", "_")
    cores = extrair_inteiro(vm_cores_label, 1)
    if "core" not in vm_cores_label.lower():
        vm_cores_label = f"{cores}_core" if cores == 1 else f"{cores}_cores"

    threads_str = escolher_opcao(
        "4. Quantas THREADS (usuarios simultaneos no JMeter) deseja enviar?",
        [
            ("10", "10 Threads (Carga Leve)"),
            ("25", "25 Threads"),
            ("50", "50 Threads (Carga Media)"),
            ("100", "100 Threads (Carga Alta)"),
            ("200", "200 Threads (Carga Muito Alta)"),
            ("500", "500 Threads (Teste de Estresse)"),
        ],
        permitir_custom=True,
    )
    threads = extrair_inteiro(threads_str, 50)

    print("\n=== 5. Confirmacao de Repeticoes, Rede e Duracao (Aperte ENTER para padrao) ===")
    rep_in = input("  Quantidade de repeticoes por linguagem [padrao: 5]: ").strip()
    repeticoes = int(rep_in) if rep_in else 5

    host = input("  IP da VM [padrao: 192.168.122.68]: ").strip() or "192.168.122.68"
    port_in = input("  Porta [padrao: 8080]: ").strip()
    port = int(port_in) if port_in else 8080

    dur_in = input("  Duracao de cada teste em segundos [padrao: 30]: ").strip()
    duration = int(dur_in) if dur_in else 30

    ramp_in = input("  Tempo de subida (Ramp-up) em segundos [padrao: 5]: ").strip()
    rampup = int(ramp_in) if ramp_in else 5

    n_in = input("  Peso do calculo de primos N [padrao: 50000]: ").strip()
    n_primos = int(n_in) if n_in else 50000

    base_dir = os.path.dirname(os.path.abspath(__file__))
    jmx_path = os.path.join(base_dir, "plano_teste.jmx")

    resultados_por_lang = {lang: [] for lang in linguagens_para_rodar}
    for lang in linguagens_para_rodar:
        for rep in range(1, repeticoes + 1):
            res = executar_bateria_linguagem(
                base_dir=base_dir,
                jmx_path=jmx_path,
                host=host,
                port=port,
                linguagem=lang,
                vm_ram_label=vm_ram_label,
                vm_cores_label=vm_cores_label,
                ram_mb=ram_mb,
                cores=cores,
                threads=threads,
                rampup=rampup,
                duration=duration,
                n_primos=n_primos,
                rep_idx=rep,
                rep_total=repeticoes,
            )
            resultados_por_lang[lang].append(res)
            time.sleep(2)

    pasta_vm = f"{vm_ram_label}_{vm_cores_label}"
    print("\n" + "=" * 85)
    print(f" RESUMO ESTATISTICO ({repeticoes} REPETICOES) — VM: {pasta_vm} | {threads} Threads")
    print("=" * 85)
    print(f"{'LINGUAGEM':<10} | {'REQ/S (Media ± DP)':<22} | {'LATENCIA ms (Media ± DP)':<24} | {'CPU MEDIA':<10} | {'RAM PICO'}")
    print("-" * 85)
    for lang, runs in resultados_por_lang.items():
        rps_vals = [r["jm"]["throughput_rps"] for r in runs]
        lat_vals = [r["jm"]["avg_ms"] for r in runs]
        cpu_vals = [r["vm"]["cpu_percent_avg"] for r in runs]
        ram_vals = [r["vm"]["process_mem_mb_max"] for r in runs]

        rps_m = statistics.mean(rps_vals)
        rps_sd = statistics.stdev(rps_vals) if len(rps_vals) > 1 else 0.0
        lat_m = statistics.mean(lat_vals)
        lat_sd = statistics.stdev(lat_vals) if len(lat_vals) > 1 else 0.0
        cpu_m = statistics.mean(cpu_vals)
        ram_m = statistics.mean(ram_vals)

        print(
            f"{lang.upper():<10} | "
            f"{rps_m:6.2f} ± {rps_sd:5.2f} req/s | "
            f"{lat_m:7.2f} ± {lat_sd:6.2f} ms    | "
            f"{cpu_m:6.2f}%   | "
            f"{ram_m:6.2f} MB"
        )
    print("=" * 85)


if __name__ == "__main__":
    main()
