# Benchmark de Desempenho: Rust vs Java vs Python (Carga de CPU + Telemetria da VM)

Este repositório contém três servidores HTTP implementados em **Rust**, **Java** e **Python** que:
1. Executam **exatamente o mesmo algoritmo CPU-bound** (verificação de números primos até `N` por divisão direta em complexidade $O(N\sqrt{N})$).
2. Monitoram **em background (a cada 1 segundo, do início ao fim)** o uso de **CPU e Memória RAM da VM e do próprio processo**, devolvendo toda a linha do tempo na rota `/metrics`.

---

## Rotas Disponíveis (Porta `8080`)

- `GET /compute?n=50000` — Rota que o **JMeter** vai atacar. Executa o cálculo de números primos até `n`.
- `GET /metrics` — Devolve um JSON completo com o **resumo (`summary`)** e o **histórico segundo a segundo (`history`)** de CPU e Memória da VM desde o início até o momento da chamada.
- `GET /metrics/reset` — Zera o cronômetro e o histórico de métricas da VM (útil para chamar imediatamente antes de apertar Play no JMeter).
- `GET /health` — Verifica se o servidor está online.

---

## Como instalar e rodar na VM

### 1. Python ([python/server.py](python/server.py)) — Usa `psutil`
```bash
sudo apt install -y python3-psutil
cd ~/analise_de_desempenho/python
python3 server.py
```

### 2. Java ([java/Server.java](java/Server.java)) — Usa `OperatingSystemMXBean` nativo
```bash
cd ~/analise_de_desempenho/java
javac Server.java
java Server
```

### 3. Rust ([rust/src/main.rs](rust/src/main.rs)) — Usa `sysinfo` (equivalente ao `psutil`)
```bash
cd ~/analise_de_desempenho/rust
cargo build --release
./target/release/benchmark-rust
```

---

## Como coletar os dados da VM na sua máquina e salvar no Banco de Dados

Logo após terminar uma bateria de testes no JMeter, rode **na sua máquina física** o script [coletar_metricas.py](coletar_metricas.py):

```bash
python3 coletar_metricas.py <IP_DA_VM> "2vcpu_4gb_100req"
```

Ele faz um `GET http://<IP_DA_VM>:8080/metrics`, exibe o resumo no terminal e salva automaticamente:
1. No banco de dados SQLite local **`resultados_vm.db`** (tabelas `execucoes` e `historico_vm` com todos os segundos do teste).
2. Uma cópia bruta em JSON na pasta **`resultados/`**.
