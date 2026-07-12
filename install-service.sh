#!/usr/bin/env bash
# Прописывает Matrix-бота как systemd-сервис на Ubuntu.
#
# Использование (запускать из директории бота или передать путь):
#   sudo bash /path/to/matrix_bot_common/install-service.sh
#   sudo bash /path/to/matrix_bot_common/install-service.sh /path/to/matrix-tiktok-bot
#
# Скрипт:
#   - определяет директорию бота и имя сервиса (matrix-<dirname>)
#   - создаёт venv (./venv) если её нет, ставит туда requirements.txt
#   - генерирует /etc/systemd/system/<service>.service
#   - daemon-reload + enable + start
#
# Переменные окружения (опц.):
#   BOT_USER=<user>   — от какого юзера запускать (по умолчанию — владелец директории бота)
#   PYTHON=<path>     — какой python использовать для venv (по умолчанию python3)

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запускайте через sudo." >&2
    exit 1
fi

BOT_DIR="${1:-$(pwd)}"
BOT_DIR="$(realpath "$BOT_DIR")"

if [[ ! -f "$BOT_DIR/bot.py" ]]; then
    echo "В $BOT_DIR не найден bot.py" >&2
    exit 1
fi

if [[ ! -f "$BOT_DIR/config.json" ]]; then
    echo "В $BOT_DIR не найден config.json. Сначала скопируйте config.example.json -> config.json и заполните." >&2
    exit 1
fi

BOT_NAME="$(basename "$BOT_DIR")"
SERVICE_NAME="matrix-${BOT_NAME#matrix-}"   # matrix-tiktok-bot -> matrix-tiktok-bot
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

BOT_USER="${BOT_USER:-$(stat -c '%U' "$BOT_DIR")}"
PYTHON="${PYTHON:-python3}"

echo "=== Установка systemd-сервиса ==="
echo "  Bot dir:    $BOT_DIR"
echo "  Service:    $SERVICE_NAME"
echo "  User:       $BOT_USER"
echo "  Python:     $PYTHON"

# 1. venv + зависимости
VENV_DIR="$BOT_DIR/venv"
if [[ ! -d "$VENV_DIR" ]]; then
    echo "--- Создаю venv ---"
    sudo -u "$BOT_USER" "$PYTHON" -m venv "$VENV_DIR"
fi

if [[ -f "$BOT_DIR/requirements.txt" ]]; then
    echo "--- Устанавливаю зависимости ---"
    sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install --upgrade pip wheel
    sudo -u "$BOT_USER" "$VENV_DIR/bin/pip" install -r "$BOT_DIR/requirements.txt"
fi

# 2. systemd unit
echo "--- Пишу $SERVICE_FILE ---"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Matrix bot: $BOT_NAME
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$BOT_USER
WorkingDirectory=$BOT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python $BOT_DIR/bot.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

# Hardening (мягкий — не трогаем $HOME, т.к. faster_whisper кэширует модели в ~/.cache)
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# 3. enable + start
echo "--- daemon-reload + enable + restart ---"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 1
systemctl --no-pager --full status "$SERVICE_NAME" || true

echo
echo "=== Готово ==="
echo "Логи:        journalctl -u $SERVICE_NAME -f"
echo "Перезапуск:  sudo systemctl restart $SERVICE_NAME"
echo "Стоп:        sudo systemctl stop $SERVICE_NAME"
echo "Снять с автозапуска: sudo systemctl disable $SERVICE_NAME"
