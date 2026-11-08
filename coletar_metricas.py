#!/usr/bin/env python3
"""
Script para rodar na SUA MAQUINA (cliente) logo apos terminar o teste do JMeter.
Ele faz uma requisicao GET para http://<IP_DA_VM>:8080/metrics, recebe todo o
historico do que aconteceu na VM (CPU e Memoria do inicio ao fim) e salva:
1. Em um banco de dados SQLite local (resultados_vm.db)
2. Em um arquivo JSON na pasta resultados/
"""

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime


def init_db(conn: sqlite3.Connection):
    cursor = conn.cursor()
    # Tabela com o resumo de cada bateria de teste
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS execucoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT,
            linguagem TEXT,
            etiqueta TEXT,
            duration_sec REAL,
            samples_count INTEGER,
            total_requests INTEGER,
            cpu_percent_avg REAL,
            cpu_percent_max REAL,
            mem_percent_avg REAL,
            mem_percent_max REAL,
            mem_used_mb_max REAL,
            mem_total_mb REAL,
            process_mem_mb_max REAL
        )
    """)
    # Tabela com o acompanhamento segundo a segundo (do inicio ao fim)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historico_vm (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execucao_id INTEGER,
            elapsed_sec REAL,
            cpu_percent REAL,
            mem_percent REAL,
            mem_used_mb REAL,
            process_mem_mb REAL,
            requests_so_far INTEGER,
            FOREIGN KEY (execucao_id) REFERENCES execucoes (id)
        )
    """)
    conn.commit()


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 coletar_metricas.py <IP_DA_VM> [etiqueta_opcional]")
        print("Exemplo: python3 coletar_metricas.py 192.168.1.50 '2vcpu_4gb_100users'")
        sys.exit(1)

    ip_vm = sys.argv[1]
    etiqueta = sys.argv[2] if len(sys.argv) >= 3 else "teste_padrao"
    port = os.environ.get("PORT", "8080")
    url = f"http://{ip_vm}:{port}/metrics"

    print(f"Buscando metricas da VM em {url} ...")
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    lang = data["lang"]
    s = data["summary"]
    history = data["history"]
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Salva no Banco de Dados SQLite
    db_path = "resultados_vm.db"
    conn = sqlite3.connect(db_path)
    init_db(conn)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO execucoes (
            data_hora, linguagem, etiqueta, duration_sec, samples_count,
            total_requests, cpu_percent_avg, cpu_percent_max,
            mem_percent_avg, mem_percent_max, mem_used_mb_max,
            mem_total_mb, process_mem_mb_max
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        agora, lang, etiqueta, s["duration_sec"], s["samples_count"],
        s["total_requests"], s["cpu_percent_avg"], s["cpu_percent_max"],
        s["mem_percent_avg"], s["mem_percent_max"], s["mem_used_mb_max"],
        s["mem_total_mb"], s["process_mem_mb_max"]
    ))
    execucao_id = cur.lastrowid

    cur.executemany("""
        INSERT INTO historico_vm (
            execucao_id, elapsed_sec, cpu_percent, mem_percent,
            mem_used_mb, process_mem_mb, requests_so_far
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            execucao_id,
            item["elapsed_sec"],
            item["cpu_percent"],
            item["mem_percent"],
            item["mem_used_mb"],
            item["process_mem_mb"],
            item["requests_so_far"],
        )
        for item in history
    ])
    conn.commit()
    conn.close()

    # 2. Salva uma copia em JSON
    os.makedirs("resultados", exist_ok=True)
    ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = f"resultados/{lang}_{etiqueta}_{ts_file}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print("\n=== RESUMO DO COMPORTAMENTO DA VM ===")
    print(f"Linguagem         : {lang.upper()}")
    print(f"Tempo monitorado  : {s['duration_sec']} s ({s['samples_count']} amostras)")
    print(f"Total requisicoes : {s['total_requests']}")
    print(f"CPU da VM         : Media {s['cpu_percent_avg']}% | Pico {s['cpu_percent_max']}%")
    print(f"RAM da VM         : Media {s['mem_percent_avg']}% | Pico {s['mem_percent_max']}% ({s['mem_used_mb_max']} MB / {s['mem_total_mb']} MB)")
    print(f"RAM do Processo   : Pico {s['process_mem_mb_max']} MB")
    print("=====================================")
    print(f"Salvo no banco SQLite '{db_path}' (execucao_id={execucao_id}) e em '{json_path}'.")


if __name__ == "__main__":
    main()
