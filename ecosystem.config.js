/**
 * PM2 Ecosystem Configuration — Command Center (Dev & Ops Portal)
 * Diretório de Produção na VPS: /var/www/command-center
 * Porta Interna: 127.0.0.1:8005 (Proxy Reverso Nginx)
 * Redis DB: 2 | PostgreSQL DB: command_center_db
 */
module.exports = {
  apps: [
    {
      name: "cc-web",
      cwd: "/var/www/command-center",
      script: "/var/www/command-center/.venv/bin/gunicorn",
      args: "config.wsgi:application --bind 127.0.0.1:8005 --workers 3 --timeout 120",
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      env: {
        DJANGO_SETTINGS_MODULE: "config.settings",
        PYTHONUNBUFFERED: "1",
      },
    },
    {
      name: "cc-celery-worker",
      cwd: "/var/www/command-center",
      script: "/var/www/command-center/.venv/bin/celery",
      args: "-A config worker -l info --concurrency=2",
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      env: {
        DJANGO_SETTINGS_MODULE: "config.settings",
        PYTHONUNBUFFERED: "1",
      },
    },
    {
      name: "cc-celery-beat",
      cwd: "/var/www/command-center",
      script: "/var/www/command-center/.venv/bin/celery",
      args: "-A config beat -l info",
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      env: {
        DJANGO_SETTINGS_MODULE: "config.settings",
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
