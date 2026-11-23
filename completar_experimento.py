#!/usr/bin/env python3
"""
Script automatizado para:
  1. Padronizar a janela de 30s de todas as execucoes ja existentes.
  2. Executar na VM todas as repeticoes faltantes ate atingir 5 execucoes completas
     por configuracao e linguagem (total: 4 cenarios x 3 linguagens x 5 repeticoes = 60 testes).
  3. Recriar o banco SQLite 'resultados/resultados_vm.db' com os 60 testes padronizados.
  4. Gerar 'resultados/estatisticas_5_repeticoes.json' e 'resultados/estatisticas_5_repeticoes.csv'
     com Media e Desvio-Padrao de todas as metricas.
"""

import csv
import glob
import json
import os
import sqlite3
import statistics
import time
from rodar_jmeter import (
    executar_bateria_linguagem,
    init_db,
    padronizar_metricas_vm,
    parse_jtl,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JMX_PATH = os.path.join(BASE_DIR, "plano_teste.jmx")
HOST = "192.168.122.68"
PORT = 8080
DURATION = 30
RAMPUP = 5
N_PRIMOS = 50000
TARGET_REPS = 5

SCENARIOS = [
    ("512MB", 512, "1_core", 1, 50),
    ("512MB", 512, "1_core", 1, 200),
    ("1024MB", 1024, "1_core", 1, 50),
    ("2048MB", 2048, "1_core", 1, 50),
]
LANGUAGES = ["rust", "java", "python"]


def padronizar_execucoes_existentes():
    print("=== [Fase 1] Padronizando janela de 30s das execucoes existentes ===")
    for ram_label, ram_mb, cores_label, cores, threads in SCENARIOS:
        pasta_vm = f"{ram_label}_{cores_label}"
        for lang in LANGUAGES:
            pattern = os.path.join(BASE_DIR, "resultados", pasta_vm, lang, f"{threads}_threads_*")
            for run_dir in sorted(glob.glob(pattern)):
                jtl_path = os.path.join(run_dir, "jmeter_requests.jtl")
                vm_path = os.path.join(run_dir, "vm_metricas_psutil.json")
                resumo_path = os.path.join(run_dir, "resumo_execucao.json")
                if not (os.path.exists(jtl_path) and os.path.exists(vm_path) and os.path.exists(resumo_path)):
                    continue

                with open(vm_path, "r", encoding="utf-8") as f:
                    vm_raw = json.load(f)
                with open(resumo_path, "r", encoding="utf-8") as f:
                    resumo = json.load(f)

                vm_summary, vm_history = padronizar_metricas_vm(vm_raw, DURATION, ram_mb)
                jm_stats = parse_jtl(jtl_path)
                vm_summary["total_requests"] = jm_stats["samples"]

                vm_standardized = {
                    "lang": lang,
                    "limits": {"cores": cores, "ram_mb": float(ram_mb)},
                    "summary": vm_summary,
                    "history": vm_history,
                }
                with open(vm_path, "w", encoding="utf-8") as f:
                    json.dump(vm_standardized, f, indent=2)

                resumo["jmeter_resultados"] = jm_stats
                resumo["vm_resumo_recursos"] = vm_summary
                with open(resumo_path, "w", encoding="utf-8") as f:
                    json.dump(resumo, f, indent=2)
                print(f"  -> Padronizado: {os.path.relpath(run_dir, BASE_DIR)} (dur={vm_summary['duration_sec']}s, cpu_avg={vm_summary['cpu_percent_avg']}%)")


def listar_runs_validas(pasta_vm: str, lang: str, threads: int) -> list[str]:
    pattern = os.path.join(BASE_DIR, "resultados", pasta_vm, lang, f"{threads}_threads_*")
    valid = []
    for d in sorted(glob.glob(pattern)):
        if (
            os.path.exists(os.path.join(d, "jmeter_requests.jtl"))
            and os.path.exists(os.path.join(d, "vm_metricas_psutil.json"))
            and os.path.exists(os.path.join(d, "resumo_execucao.json"))
        ):
            valid.append(d)
    return valid


def executar_faltantes():
    print("\n=== [Fase 2] Executando repeticoes faltantes na VM ===")
    for ram_label, ram_mb, cores_label, cores, threads in SCENARIOS:
        pasta_vm = f"{ram_label}_{cores_label}"
        for lang in LANGUAGES:
            valid_runs = listar_runs_validas(pasta_vm, lang, threads)
            atual = len(valid_runs)
            faltam = max(0, TARGET_REPS - atual)
            print(f"\n-> Cenario {pasta_vm} | {lang.upper()} | {threads} threads: {atual}/{TARGET_REPS} prontos ({faltam} faltantes)")
            for i in range(atual + 1, TARGET_REPS + 1):
                executar_bateria_linguagem(
                    base_dir=BASE_DIR,
                    jmx_path=JMX_PATH,
                    host=HOST,
                    port=PORT,
                    linguagem=lang,
                    vm_ram_label=ram_label,
                    vm_cores_label=cores_label,
                    ram_mb=ram_mb,
                    cores=cores,
                    threads=threads,
                    rampup=RAMPUP,
                    duration=DURATION,
                    n_primos=N_PRIMOS,
                    rep_idx=i,
                    rep_total=TARGET_REPS,
                )
                time.sleep(2)


def reconstruir_banco_e_estatisticas():
    print("\n=== [Fase 3] Reconstruindo banco SQLite e calculando Media / Desvio-Padrao ===")
    db_path = os.path.join(BASE_DIR, "resultados", "resultados_vm.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)
    init_db(conn)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS estatisticas_agregadas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pasta_vm TEXT,
            linguagem TEXT,
            threads INTEGER,
            repeticoes INTEGER,
            throughput_mean REAL,
            throughput_stdev REAL,
            samples_mean REAL,
            samples_stdev REAL,
            avg_ms_mean REAL,
            avg_ms_stdev REAL,
            p95_ms_mean REAL,
            p95_ms_stdev REAL,
            error_pct_mean REAL,
            cpu_avg_mean REAL,
            cpu_avg_stdev REAL,
            cpu_max_mean REAL,
            ram_mb_mean REAL,
            ram_mb_stdev REAL
        )
    """)

    estatisticas_lista = []

    for ram_label, ram_mb, cores_label, cores, threads in SCENARIOS:
        pasta_vm = f"{ram_label}_{cores_label}"
        for lang in LANGUAGES:
            valid_runs = listar_runs_validas(pasta_vm, lang, threads)[:TARGET_REPS]
            resumos = []
            for run_dir in valid_runs:
                with open(os.path.join(run_dir, "resumo_execucao.json"), "r", encoding="utf-8") as f:
                    resumo = json.load(f)
                with open(os.path.join(run_dir, "vm_metricas_psutil.json"), "r", encoding="utf-8") as f:
                    vm_data = json.load(f)

                resumos.append(resumo)
                jm = resumo["jmeter_resultados"]
                vm = resumo["vm_resumo_recursos"]

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
                    resumo["data_hora"], pasta_vm, ram_label, cores_label, lang,
                    threads, RAMPUP, DURATION, N_PRIMOS,
                    jm["samples"], jm["errors"], jm["error_pct"], jm["throughput_rps"],
                    jm["avg_ms"], jm["min_ms"], jm["p95_ms"], jm["max_ms"],
                    vm["duration_sec"], vm["cpu_percent_avg"], vm["cpu_percent_max"],
                    vm["mem_percent_avg"], vm["mem_percent_max"],
                    vm["mem_used_mb_max"], vm["mem_total_mb"], vm["process_mem_mb_max"],
                    os.path.relpath(run_dir, BASE_DIR),
                ))
                teste_id = cur.lastrowid

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
                    for item in vm_data.get("history", [])
                ])

            def calc_ms(vals):
                if not vals:
                    return 0.0, 0.0
                m = round(statistics.mean(vals), 2)
                sd = round(statistics.stdev(vals), 2) if len(vals) > 1 else 0.0
                return m, sd

            rps_m, rps_sd = calc_ms([r["jmeter_resultados"]["throughput_rps"] for r in resumos])
            smp_m, smp_sd = calc_ms([r["jmeter_resultados"]["samples"] for r in resumos])
            avg_m, avg_sd = calc_ms([r["jmeter_resultados"]["avg_ms"] for r in resumos])
            p95_m, p95_sd = calc_ms([r["jmeter_resultados"]["p95_ms"] for r in resumos])
            err_m, _ = calc_ms([r["jmeter_resultados"]["error_pct"] for r in resumos])
            cpu_m, cpu_sd = calc_ms([r["vm_resumo_recursos"]["cpu_percent_avg"] for r in resumos])
            cpumax_m, _ = calc_ms([r["vm_resumo_recursos"]["cpu_percent_max"] for r in resumos])
            ram_m, ram_sd = calc_ms([r["vm_resumo_recursos"]["process_mem_mb_max"] for r in resumos])

            stat_row = {
                "pasta_vm": pasta_vm,
                "ram_mb": ram_mb,
                "cores": cores,
                "linguagem": lang,
                "threads": threads,
                "repeticoes": len(resumos),
                "throughput_rps_mean": rps_m,
                "throughput_rps_stdev": rps_sd,
                "samples_mean": smp_m,
                "samples_stdev": smp_sd,
                "avg_ms_mean": avg_m,
                "avg_ms_stdev": avg_sd,
                "p95_ms_mean": p95_m,
                "p95_ms_stdev": p95_sd,
                "error_pct_mean": err_m,
                "cpu_avg_mean": cpu_m,
                "cpu_avg_stdev": cpu_sd,
                "cpu_max_mean": cpumax_m,
                "ram_mb_mean": ram_m,
                "ram_mb_stdev": ram_sd,
            }
            estatisticas_lista.append(stat_row)

            cur.execute("""
                INSERT INTO estatisticas_agregadas (
                    pasta_vm, linguagem, threads, repeticoes,
                    throughput_mean, throughput_stdev, samples_mean, samples_stdev,
                    avg_ms_mean, avg_ms_stdev, p95_ms_mean, p95_ms_stdev,
                    error_pct_mean, cpu_avg_mean, cpu_avg_stdev, cpu_max_mean,
                    ram_mb_mean, ram_mb_stdev
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pasta_vm, lang, threads, len(resumos),
                rps_m, rps_sd, smp_m, smp_sd,
                avg_m, avg_sd, p95_m, p95_sd,
                err_m, cpu_m, cpu_sd, cpumax_m,
                ram_m, ram_sd,
            ))

    conn.commit()
    conn.close()

    json_stats_path = os.path.join(BASE_DIR, "resultados", "estatisticas_5_repeticoes.json")
    with open(json_stats_path, "w", encoding="utf-8") as f:
        json.dump(estatisticas_lista, f, indent=2)

    csv_stats_path = os.path.join(BASE_DIR, "resultados", "estatisticas_5_repeticoes.csv")
    if estatisticas_lista:
        with open(csv_stats_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(estatisticas_lista[0].keys()))
            writer.writeheader()
            writer.writerows(estatisticas_lista)

    print("-> Estatisticas salvas em:")
    print(f"   {json_stats_path}")
    print(f"   {csv_stats_path}")
    print(f"   {db_path}")


if __name__ == "__main__":
    padronizar_execucoes_existentes()
    executar_faltantes()
    reconstruir_banco_e_estatisticas()
