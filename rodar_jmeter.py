#!/usr/bin/env python3
"""
Script interativo para rodar os testes do JMeter (sem interface grafica),
perguntando no inicio:
  1. Qual linguagem sera testada (Rust, Java ou Python)
  2. Qual a configuracao atual da VM (512MB, 1024MB, 2048MB e quantidade de Cores)
  3. Qual a opcao de Threads (usuarios simultaneos no JMeter)

Organiza automaticamente os resultados na estrutura de pastas:
  resultados/
    └── <RAM>_<CORES>/              (ex: 512MB_1_core, 1024MB_2_cores, 2048MB_4_cores)
          └── <linguagem>/          (rust, java, python)
                └── <N>_threads/    (ex: 50_threads_20261108_190500)
                      ├── jmeter_requests.jtl
                      ├── vm_metricas_psutil.json
                      ├── resumo_execucao.json
                      └── dashboard_html/index.html
"""

import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
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
            -- Metricas do JMeter (Cliente)
            jmeter_samples INTEGER,
            jmeter_errors INTEGER,
            jmeter_error_pct REAL,
            jmeter_throughput_rps REAL,
            jmeter_avg_ms REAL,
            jmeter_min_ms REAL,
            jmeter_p95_ms REAL,
            jmeter_max_ms REAL,
            -- Metricas da VM (Servidor - psutil / sysinfo / OSBean)
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


def main():
    print("=" * 60)
    print("   CONFIGURADOR INTERATIVO DE BENCHMARK (JMeter + VM)")
    print("=" * 60)

    # 1. Escolha da Linguagem
    linguagem = escolher_opcao(
        "1. Qual LINGUAGEM voce vai testar agora?",
        [
            ("rust", "Rust"),
            ("java", "Java"),
            ("python", "Python"),
        ],
    )

    # 2. Escolha da Memoria RAM configurada na VM
    vm_ram = escolher_opcao(
        "2. Qual a capacidade de MEMORIA RAM configurada na VM?",
        [
            ("512MB", "512 MB"),
            ("1024MB", "1.024 MB (1 GB)"),
            ("2048MB", "2.048 MB (2 GB)"),
        ],
        permitir_custom=True,
    ).replace(" ", "")

    # 3. Escolha da quantidade de Cores de CPU na VM
    vm_cores = escolher_opcao(
        "3. Quantos CORES (vCPUs) estao configurados na VM?",
        [
            ("1_core", "1 Core (1 vCPU)"),
            ("2_cores", "2 Cores (2 vCPUs)"),
            ("4_cores", "4 Cores (4 vCPUs)"),
        ],
        permitir_custom=True,
    ).replace(" ", "_")

    # 4. Escolha da opcao de Threads (usuarios simultaneos no JMeter)
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
    threads = int(threads_str)

    # 5. Parametros de Rede e Duracao (com valores padrao rapidos dando Enter)
    print("\n=== 5. Confirmacao de Rede e Duracao (Aperte ENTER para usar o padrao) ===")
    host = input("  IP da VM [padrao: 192.168.122.68]: ").strip() or "192.168.122.68"
    port_in = input("  Porta [padrao: 8080]: ").strip()
    port = int(port_in) if port_in else 8080

    dur_in = input("  Duracao do teste em segundos [padrao: 30]: ").strip()
    duration = int(dur_in) if dur_in else 30

    ramp_in = input("  Tempo de subida (Ramp-up) em segundos [padrao: 5]: ").strip()
    rampup = int(ramp_in) if ramp_in else 5

    n_in = input("  Peso do calculo de primos N [padrao: 50000]: ").strip()
    n_primos = int(n_in) if n_in else 50000

    # Monta a estrutura de pastas solicitada:
    # resultados/<RAM>_<CORES>/<linguagem>/<threads>_threads_<timestamp>/
    base_dir = os.path.dirname(os.path.abspath(__file__))
    jmx_path = os.path.join(base_dir, "plano_teste.jmx")
    pasta_vm = f"{vm_ram}_{vm_cores}"
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

    print("\n" + "-" * 60)
    print(f"Pasta de destino : resultados/{pasta_vm}/{linguagem}/{threads}_threads_{ts_str}/")
    print("-" * 60)

    # Verifica se o servidor esta online na VM e se bate com a linguagem escolhida
    print(f"[1/4] Verificando conexao com a VM em {base_url}/health ...")
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            lang_ativa = health.get("lang", "").lower()
    except Exception as e:
        print(f"\n[ERRO] Nao foi possivel conectar na VM em {base_url}/health: {e}")
        print(f"Certifique-se de que voce iniciou o servidor de {linguagem.upper()} la na VM!")
        sys.exit(1)

    if lang_ativa and lang_ativa != linguagem:
        print(f"\n[ATENCAO] Voce escolheu '{linguagem.upper()}', mas quem esta ligado na VM agora e '{lang_ativa.upper()}'!")
        conf = input("Deseja continuar mesmo assim? (s/N): ").strip().lower()
        if conf != "s":
            print("Teste cancelado. Ligue a linguagem correta na VM e rode novamente.")
            sys.exit(1)

    # Zera as metricas na VM
    print(f"[2/4] Zerando acompanhamento de CPU e RAM na VM ({base_url}/metrics/reset) ...")
    try:
        with urllib.request.urlopen(f"{base_url}/metrics/reset", timeout=5) as resp:
            resp.read()
    except Exception as e:
        print(f"      Aviso: /metrics/reset nao respondeu ({e}).")

    # Executa o JMeter sem interface grafica
    jtl_path = os.path.join(pasta_execucao, "jmeter_requests.jtl")
    dashboard_dir = os.path.join(pasta_execucao, "dashboard_html")

    cmd = get_jmeter_cmd() + [
        "-n",
        "-t", jmx_path,
        "-l", jtl_path,
        "-e", "-o", dashboard_dir,
        f"-Jhost={host}",
        f"-Jport={port}",
        f"-Jthreads={threads}",
        f"-Jrampup={rampup}",
        f"-Jduration={duration}",
        f"-Jn={n_primos}",
    ]

    print(f"[3/4] Executando JMeter ({threads} threads por {duration}s contra {linguagem.upper()})...")
    subprocess.run(cmd, check=True)

    # Coleta as metricas da VM do inicio ao fim
    print(f"[4/4] Coletando dados de CPU e Memoria da VM ({base_url}/metrics) ...")
    vm_summary = {
        "duration_sec": 0.0, "samples_count": 0, "total_requests": 0,
        "cpu_percent_avg": 0.0, "cpu_percent_max": 0.0,
        "mem_percent_avg": 0.0, "mem_percent_max": 0.0,
        "mem_used_mb_max": 0.0, "mem_total_mb": 0.0, "process_mem_mb_max": 0.0,
    }
    vm_history = []
    try:
        with urllib.request.urlopen(f"{base_url}/metrics", timeout=15) as resp:
            vm_data = json.loads(resp.read().decode("utf-8"))
            vm_summary = vm_data.get("summary", vm_summary)
            vm_history = vm_data.get("history", [])

            vm_json_path = os.path.join(pasta_execucao, "vm_metricas_psutil.json")
            with open(vm_json_path, "w", encoding="utf-8") as f:
                json.dump(vm_data, f, indent=2)
    except Exception as e:
        print(f"      Aviso: Nao foi possivel coletar /metrics da VM: {e}")

    jm_stats = parse_jtl(jtl_path)

    # Salva um JSON de resumo consolidado dentro da pasta da linguagem
    resumo_consolidado = {
        "data_hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "configuracao_vm": {
            "pasta_vm": pasta_vm,
            "ram": vm_ram,
            "cores": vm_cores,
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

    # Salva tambem no Banco de Dados SQLite central dentro de resultados/resultados_vm.db
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
        resumo_consolidado["data_hora"], pasta_vm, vm_ram, vm_cores, linguagem,
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

    print("\n" + "=" * 60)
    print(f" TESTE FINALIZADO COM SUCESSO!")
    print("=" * 60)
    print(f" Configuracao VM   : {pasta_vm} (RAM real detectada: {vm_summary['mem_total_mb']} MB)")
    print(f" Linguagem         : {linguagem.upper()} | Threads JMeter: {threads}")
    print("-" * 60)
    print(f" [JMeter] Requisicoes Respondidas : {jm_stats['samples']} (Erros: {jm_stats['error_pct']}%)")
    print(f" [JMeter] Throughput (Vazao)      : {jm_stats['throughput_rps']} req/s")
    print(f" [JMeter] Tempo de Resposta (ms)  : Media={jm_stats['avg_ms']}ms | p95={jm_stats['p95_ms']}ms | Max={jm_stats['max_ms']}ms")
    print("-" * 60)
    print(f" [VM] Uso de CPU (Inicio ao Fim)  : Media={vm_summary['cpu_percent_avg']}% | Pico={vm_summary['cpu_percent_max']}%")
    print(f" [VM] Uso de RAM da VM            : Media={vm_summary['mem_percent_avg']}% | Pico={vm_summary['mem_percent_max']}% ({vm_summary['mem_used_mb_max']} MB)")
    print(f" [VM] Uso de RAM da Linguagem     : Pico={vm_summary['process_mem_mb_max']} MB")
    print("=" * 60)
    print(f" Arquivos salvos em:\n   -> {pasta_execucao}/")
    print(f"   -> Banco SQLite geral: {db_path}")


if __name__ == "__main__":
    main()
