# Benchmark de Desempenho: Rust vs Java vs Python (Carga de CPU com JMeter)

Este repositório contém três servidores HTTP implementados em **Rust**, **Java** e **Python** que executam **exatamente o mesmo algoritmo CPU-bound** (verificação de números primos até `N` por divisão direta em complexidade $O(N\sqrt{N})$).

## Endpoints

Todos os servidores escutam por padrão na porta `8080` (configurável via variável de ambiente `PORT`):

- `GET /compute?n=50000` — Executa o cálculo de números primos de `2` até `n` (padrão: `50000`) e retorna:
  ```json
  {
    "lang": "rust | java | python",
    "n": 50000,
    "primes": 5133,
    "elapsed_ms": 4.125
  }
  ```
- `GET /health` — Endpoint leve para verificar se o servidor está online.

> **Dica para o JMeter:** Você pode variar a intensidade de CPU por requisição direto pelo JMeter mudando apenas o parâmetro `?n=` (ex.: `n=10000` para carga leve, `n=100000` para carga moderada, `n=300000` para carga pesada), sem precisar recompilar nenhum código.

---

## Como rodar cada linguagem na VM

> **Importante:** Rode um servidor por vez na VM (ou em portas separadas) para que um não dispute CPU/memória com o outro durante a bateria de testes.

### 1. Rust (`rust/`)
Sempre compile e execute em modo `--release` para ativar as otimizações do LLVM (`opt-level = 3` e `lto = true`):
```bash
cd rust
cargo build --release
PORT=8080 ./target/release/benchmark-rust
```
Para limitar a quantidade de threads do Tokio na VM (ex.: testar escalabilidade com 1, 2, 4 núcleos):
```bash
TOKIO_WORKER_THREADS=4 PORT=8080 ./target/release/benchmark-rust
```

### 2. Java (`java/`)
Usa a biblioteca nativa `com.sun.net.httpserver.HttpServer` do JDK (zero dependências externas).
```bash
cd java
javac Server.java
PORT=8080 THREADS=8 java -Xms512m -Xmx2g Server
```
*(A variável `THREADS` define o tamanho do pool de threads. Por padrão usa `2 * núcleos da VM`).*

### 3. Python (`python/`)
Devido ao **GIL (Global Interpreter Lock)** do CPython, um único processo Python usa apenas 1 núcleo de CPU por vez em tarefas CPU-bound. Você pode testar dois cenários:

- **Cenário A — Processo único (impacto do GIL):**
  ```bash
  cd python
  PORT=8080 python3 server.py
  ```
- **Cenário B — Múltiplos processos workers (Uvicorn/Gunicorn usando todos os núcleos da VM):**
  ```bash
  cd python
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  uvicorn server:app --host 0.0.0.0 --port 8080 --workers 4 --no-access-log
  ```

---

## Ajustes Recomendados na VM Linux antes do Teste com JMeter

Quando o JMeter dispara centenas ou milhares de requisições simultâneas, o próprio sistema operacional pode rejeitar conexões antes mesmo de a linguagem ser testada. Ajuste na VM:

1. **Aumentar o limite de descritores de arquivos (sockets abertos):**
   ```bash
   ulimit -n 65535
   ```
2. **Aumentar a fila de conexões TCP pendentes (backlog) e reutilização de portas:**
   ```bash
   sudo sysctl -w net.core.somaxconn=4096
   sudo sysctl -w net.ipv4.tcp_max_syn_backlog=4096
   sudo sysctl -w net.ipv4.tcp_tw_reuse=1
   ```
3. **Limitar núcleos de CPU na VM (opcional, ótimo para o relatório):**
   Você pode usar o comando `taskset` no Linux para forçar o servidor a rodar em apenas 1, 2 ou 4 núcleos sem precisar reiniciar a VM:
   ```bash
   # Exemplo: rodar apenas no nucleo 0 (1 vCPU)
   taskset -c 0 ./target/release/benchmark-rust

   # Exemplo: rodar nos nucleos 0 e 1 (2 vCPUs)
   taskset -c 0,1 java Server
   ```
