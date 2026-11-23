# Relatório Técnico: Ambiente Experimental, Arquitetura dos Servidores, Metodologia e Resultados (5 Repetições)

## 1. Informações do Computador Principal (Máquina Host / Gerador de Carga)
Máquina física responsável por disparar as requisições HTTP via **Apache JMeter** e consolidar os resultados:

- **Sistema Operacional:** Fedora Linux 44 (Workstation Edition) — 64 bits
- **Kernel Linux:** `7.2.15-200.fc44.x86_64`
- **Processador (CPU):** Intel Core i5-10300H @ 2.50 GHz (4 núcleos físicos / 8 threads lógicas)
- **Memória RAM Total:** 16 GB (15 GiB utilizáveis + 8 GB de Swap)
- **Ferramenta de Carga:** Apache JMeter 5.6.3 (executado em modo CLI `-n`, sem interface gráfica)

---

## 2. Informações da Máquina Virtual (VM / Servidor Alvo)
Ambiente virtualizado isolado onde rodam os servidores em **Rust**, **Java** e **Python**:

- **Virtualizador (Hypervisor):** QEMU / KVM (gerenciado via `libvirt`, rede `virbr0`, IP `192.168.122.68`)
- **Sistema Operacional da VM:** Ubuntu Linux 25.10 (64 bits)
- **Controle de Recursos no Kernel Linux da VM ([controlador_vm.py](controlador_vm.py)):**
  - **Memória RAM (`512 MB`, `1.024 MB`, `2.048 MB`):** Restringida via **Linux cgroups v2 (`systemd-run --scope -p MemoryMax=... -p MemorySwapMax=0`)** e limite de Heap na JVM (`-Xms64m -Xmx<80% da RAM>m`).
  - **Núcleos de CPU (`1 Core`):** Restringidos via **`taskset -c 0`** e `AllowedCPUs=0`, fixando a execução estritamente em 1 núcleo lógico da VM.

---

## 3. Arquitetura dos Servidores: Frameworks, Workers e Explicação de Desempenho

Os três servidores implementam exatamente o mesmo algoritmo CPU-bound (`GET /compute?n=50000`), que verifica todos os números primos de `2` até `50.000` por divisão direta até $\sqrt{k}$ (complexidade $\mathcal{O}(N\sqrt{N})$, encontrando 5.133 primos a cada requisição).

| Linguagem | Arquivo Fonte | Framework / Servidor HTTP | Workers / Modelo de Concorrência (em 1 Core) | Compilação e Gerenciamento de Memória |
| :--- | :--- | :--- | :--- | :--- |
| **Rust** | [rust/src/main.rs](rust/src/main.rs) | **Axum 0.7** sobre **Tokio 1.x** (Hyper) | **1 Event-Loop Worker** (`TOKIO_WORKER_THREADS=1`) para I/O assíncrono + pool de threads `tokio::task::spawn_blocking` para o cálculo CPU-bound | Compilado nativamente para código de máquina (`cargo build --release`, `opt-level=3`, `lto=true`), sem *Garbage Collector* |
| **Java** | [java/Server.java](java/Server.java) | **`com.sun.net.httpserver.HttpServer`** (Nativo do JDK) | **Pool fixo de 2 Worker Threads** (`Executors.newFixedThreadPool(max(2, cores * 2))`) com fila de backlog TCP de `4096` conexões | Compilado em bytecode e otimizado em tempo de execução pelo **JIT (HotSpot C2)** da JVM; memória gerenciada por Garbage Collector |
| **Python** | [python/server.py](python/server.py) | **`http.server.ThreadingHTTPServer`** (Biblioteca padrão do Python) | **1 único processo (`single-process`)** que instancia **1 thread por requisição** (`ThreadingMixIn`), todas disputando o **GIL (*Global Interpreter Lock*)** | Interpretado em bytecode pela CPython VM, sem compilação JIT nativa |

### Por que o Python é ~20x mais lento e apresenta erros em 200 threads?
1. **Custo de interpretação vs. código nativo/JIT:** Enquanto o código de máquina do Rust e o código JIT do Java executam o teste de primalidade de `n=50000` em ~`3 ms` usando registradores de hardware da CPU, o interpretador CPython leva ~`65 a 95 ms` por requisição devido à checagem dinâmica de tipos e criação de objetos inteiros a cada divisão.
2. **Efeito do GIL (*Global Interpreter Lock*) no `ThreadingHTTPServer`:** Como o servidor Python roda em **1 processo único** com threads, todas as 50 ou 200 threads criadas pelo `ThreadingHTTPServer` disputam o **GIL**. Apenas 1 thread pode executar bytecode Python por vez, e a constante disputa/troca de contexto entre 200 threads faz o tempo de fila explodir (latência média sobe para `4.677 ms` e `p95` bate no `response_timeout` de `10.000 ms`, gerando `44,63%` de timeout em 200 threads).

---

## 4. Padronização da Janela de Medição (30 Segundos) e Repetições

1. **Padronização da Janela de 30s ([rodar_jmeter.py](rodar_jmeter.py)):**
   - O JMeter (executado via Flatpak/JVM) leva ~`3 segundos` para inicializar antes de disparar a primeira requisição HTTP, e servidores sobrecarregados (como o Python) podem levar alguns segundos extras esvaziando conexões residuais após o término.
   - Para eliminar essa distorção, a função `padronizar_metricas_vm` recorta exatamente as **30 amostras de 1 segundo (`duration_sec = 30.0s`, `samples_count = 30`)** a partir do primeiro segundo de carga ativa (`requests_so_far > 0` ou `cpu_percent > 5%`). Além disso, o arquivo [plano_teste.jmx](plano_teste.jmx) define `connect_timeout = 5000 ms` e `response_timeout = 10000 ms`.
2. **Rigor Estatístico (5 Repetições por Cenário):**
   - Cada combinação de `(Memória, Cores, Threads, Linguagem)` foi executada **5 vezes** de forma independente (totalizando **60 execuções completas**), calculando-se a **Média ($\mu$)** e o **Desvio-Padrão amostral ($s$)** em [resultados/estatisticas_5_repeticoes.csv](resultados/estatisticas_5_repeticoes.csv) e [resultados/estatisticas_5_repeticoes.json](resultados/estatisticas_5_repeticoes.json).

---

## 5. Resultados Consolidados (Média ± Desvio-Padrão de 5 Repetições)

| Cenário da VM | Threads | Linguagem | Vazão / Throughput (req/s) | Total de Requisições (30s) | Latência Média (ms) | Latência p95 (ms) | Taxa de Erro (%) | CPU Média na Janela de 30s (%) | Pico de RAM do Processo (MB) |
| :--- | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **512 MB / 1 Core** | 50 | **Rust** | **321,30 ± 10,50** | 9.705,2 ± 322,7 | **142,45 ± 4,92** | 177,00 ± 15,25 | 0,00% | 95,29 ± 1,13% | **10,66 ± 0,65 MB** |
| **512 MB / 1 Core** | 50 | **Java** | 284,58 ± 7,46 | 8.595,6 ± 226,5 | 160,84 ± 4,18 | 223,60 ± 9,84 | 0,00% | 93,92 ± 1,81% | 101,44 ± 0,55 MB |
| **512 MB / 1 Core** | 50 | **Python** | 14,95 ± 3,43 | 714,6 ± 23,0 | 2.023,64 ± 89,85 | 5.608,40 ± 818,10 | 6,32% | 98,49 ± 0,73% | 21,91 ± 0,63 MB |
| **512 MB / 1 Core** | 200 | **Rust** | **318,57 ± 6,66** | 9.919,6 ± 222,3 | **561,81 ± 12,28** | 679,00 ± 25,44 | 0,00% | 95,62 ± 2,43% | **15,35 ± 0,93 MB** |
| **512 MB / 1 Core** | 200 | **Java** | 284,52 ± 14,98 | 8.858,4 ± 453,5 | 630,16 ± 32,92 | 842,80 ± 45,98 | 0,00% | 95,13 ± 0,86% | 109,04 ± 0,50 MB |
| **512 MB / 1 Core** | 200 | **Python** | 25,08 ± 1,14 | 1.312,8 ± 26,5 | 4.677,36 ± 108,03 | 13.075,00 ± 7,48 | 44,63% | 98,62 ± 0,15% | 22,31 ± 0,04 MB |
| **1024 MB / 1 Core** | 50 | **Rust** | **330,78 ± 6,79** | 9.996,4 ± 189,4 | **138,21 ± 2,67** | 163,60 ± 6,19 | 0,00% | 96,34 ± 1,35% | **10,73 ± 0,83 MB** |
| **1024 MB / 1 Core** | 50 | **Java** | 294,29 ± 7,42 | 8.885,8 ± 225,8 | 155,60 ± 4,11 | 213,80 ± 7,40 | 0,00% | 93,79 ± 2,56% | 101,66 ± 0,62 MB |
| **1024 MB / 1 Core** | 50 | **Python** | 15,48 ± 3,79 | 728,4 ± 31,8 | 1.995,88 ± 142,83 | 6.114,00 ± 879,50 | 5,81% | 98,62 ± 0,69% | 21,93 ± 0,60 MB |
| **2048 MB / 1 Core** | 50 | **Rust** | **332,41 ± 7,45** | 10.041,4 ± 221,7 | **137,60 ± 3,14** | 162,40 ± 8,73 | 0,00% | 95,55 ± 2,85% | **10,56 ± 0,63 MB** |
| **2048 MB / 1 Core** | 50 | **Java** | 284,65 ± 11,68 | 8.599,6 ± 350,4 | 160,90 ± 6,49 | 223,80 ± 11,88 | 0,00% | 93,89 ± 2,18% | 100,90 ± 2,38 MB |
| **2048 MB / 1 Core** | 50 | **Python** | 14,73 ± 3,71 | 710,6 ± 28,2 | 2.051,33 ± 123,58 | 5.742,20 ± 1.032,83 | 6,39% | 98,59 ± 0,32% | 21,93 ± 0,64 MB |

---

## 6. Estrutura das Pastas e Arquivos Gerados

```text
analise_de_desempenho/
├── rust/                                    # Código-fonte do servidor Rust (Axum + Tokio + sysinfo)
├── java/                                    # Código-fonte do servidor Java (HttpServer + OSBean)
├── python/                                  # Código-fonte do servidor Python (ThreadingHTTPServer + psutil)
├── controlador_vm.py                        # Agente na VM que ativa/desativa linguagens e aplica limites (cgroups/taskset)
├── plano_teste.jmx                          # Plano de testes parametrizado do Apache JMeter 5.6.3
├── rodar_jmeter.py                          # Orquestrador interativo (suporta N repetições e calcula Média/DP)
├── completar_experimento.py                 # Script de automação da bateria completa (60 execuções) e geração de tabelas
├── METODOLOGIA_E_AMBIENTE.md                # Este documento técnico completo
└── resultados/
    ├── estatisticas_5_repeticoes.csv        # Tabela pronta (Excel/LibreOffice) com Média e Desvio-Padrão
    ├── estatisticas_5_repeticoes.json       # Dados agregados (Média e Desvio-Padrão) em JSON
    ├── resultados_vm.db                     # Banco SQLite com todas as 60 execuções + tabela estatisticas_agregadas
    ├── 512MB_1_core/
    │   ├── rust/   (5 pastas de 50_threads + 5 pastas de 200_threads)
    │   ├── java/   (5 pastas de 50_threads + 5 pastas de 200_threads)
    │   └── python/ (5 pastas de 50_threads + 5 pastas de 200_threads)
    ├── 1024MB_1_core/
    │   ├── rust/   (5 pastas de 50_threads)
    │   ├── java/   (5 pastas de 50_threads)
    │   └── python/ (5 pastas de 50_threads)
    └── 2048MB_1_core/
        ├── rust/   (5 pastas de 50_threads)
        ├── java/   (5 pastas de 50_threads)
        └── python/ (5 pastas de 50_threads)
```
