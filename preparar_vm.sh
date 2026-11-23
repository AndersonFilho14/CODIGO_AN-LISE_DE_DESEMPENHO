#!/usr/bin/env bash
# Script para preparar a VM uma unica vez!
# Ele envia os arquivos para a VM, instala FastAPI+Uvicorn, Maven (Spring Boot) e Rust (Axum),
# compila os servidores e configura o servico systemd automatico.

set -e

VM_USER="${1:-user}"
VM_IP="${2:-192.168.122.68}"
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo " 1. Enviando arquivos atualizados para ${VM_USER}@${VM_IP}..."
echo "============================================================"
ssh "${VM_USER}@${VM_IP}" "mkdir -p ~/analise_de_desempenho"
scp -r "${BASE_DIR}/python" "${BASE_DIR}/java" "${BASE_DIR}/rust" "${BASE_DIR}/controlador_vm.py" "${VM_USER}@${VM_IP}:~/analise_de_desempenho/"

echo "============================================================"
echo " 2. Configurando compilacao e servico automatico na VM..."
echo "============================================================"
ssh -t "${VM_USER}@${VM_IP}" "bash -c '
  set -e
  sudo apt update
  sudo apt install -y python3 python3-venv python3-pip python3-psutil default-jdk maven curl build-essential cargo rustc
  sudo ufw allow 8080/tcp || true
  sudo ufw allow 9000/tcp || true

  echo \"-> Configurando ambiente Python (FastAPI + Uvicorn)...\"
  cd ~/analise_de_desempenho/python
  python3 -m venv venv
  ./venv/bin/pip install --upgrade pip
  ./venv/bin/pip install -r requirements.txt

  echo \"-> Compilando servidor Java (Spring Boot 3 via Maven)...\"
  cd ~/analise_de_desempenho/java
  mvn clean package -DskipTests

  echo \"-> Compilando servidor Rust (Axum + Tokio em modo release)...\"
  cd ~/analise_de_desempenho/rust
  if [ -f \"\$HOME/.cargo/env\" ]; then source \"\$HOME/.cargo/env\"; fi
  cargo build --release

  echo \"-> Ativando servico controlador automatico no boot da VM...\"
  sudo tee /etc/systemd/system/benchmark-controller.service > /dev/null <<EOF
[Unit]
Description=Controlador Automatico de Benchmark (Rust/Axum, Java/Spring, Python/Uvicorn)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/${VM_USER}/analise_de_desempenho
ExecStart=/usr/bin/python3 /home/${VM_USER}/analise_de_desempenho/controlador_vm.py
Restart=always
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable benchmark-controller.service
  sudo systemctl restart benchmark-controller.service
  echo \"-> Tudo pronto na VM!\"
'"

echo ""
echo "============================================================"
echo " VM PREPARADA COM SUCESSO!"
echo " Agora todos os servidores usam as stacks padrao de mercado:"
echo "  - Rust:   Axum + Tokio + Serde"
echo "  - Java:   Spring Boot 3 (Tomcat + Virtual Threads + Jackson)"
echo "  - Python: FastAPI + Uvicorn"
echo " Basta rodar: python3 rodar_jmeter.py"
echo "============================================================"
