#!/usr/bin/env bash
# ==============================================================================
# Script de Deploy Automatizado na VPS Ubuntu — Command Center (Dev & Ops Portal)
# Caminho: /var/www/command-center | Porta: 8005 | PostgreSQL: command_center_db
# ==============================================================================
set -euo pipefail

APP_DIR="/var/www/command-center"
cd "$APP_DIR"

echo "==> [1/5] Atualizando código-fonte via Git..."
git pull origin main

echo "==> [2/5] Ativando virtualenv (.venv) e instalando dependências..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> [3/5] Aplicando migrações do banco de dados PostgreSQL..."
python manage.py migrate --noinput

echo "==> [4/5] Coletando arquivos estáticos (WhiteNoise)..."
python manage.py collectstatic --noinput

echo "==> [5/5] Recarregando processos no PM2 (cc-web, cc-celery-worker, cc-celery-beat)..."
pm2 startOrReload ecosystem.config.js --update-env
pm2 save

echo "==> Deploy do Command Center concluído com sucesso em 127.0.0.1:8005!"
