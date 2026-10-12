FROM python:3.11-slim

WORKDIR /app

# Директория для постоянных данных: БД, файлы состояния, логи.
# Монтируется как Docker volume — данные сохраняются при перезапуске.
# В коде бота используйте: import os; DATA_DIR = os.getenv('DATA_DIR', '/app/data')
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data && chmod 777 /app/data
RUN chown -R $(id -u):$(id -g) /app/data 2>/dev/null || chown -R 1000:1000 /app/data || true

# Устанавливаем зависимости
RUN pip install --no-cache-dir \
    "aiogram>=3.0.0" \
    aiohttp \
    pyTelegramBotAPI \
    requests \
    python-dotenv \
    aiosqlite


# Очищаем pip кеш
RUN pip cache purge || true

# Копируем код приложения
COPY . .

# Создаем entrypoint скрипт для инициализации прав на /app/data
RUN echo '#!/bin/sh' > /usr/local/bin/entrypoint.sh && \
    echo 'set -e' >> /usr/local/bin/entrypoint.sh && \
    echo '# Инициализация прав на /app/data (важно для volume)' >> /usr/local/bin/entrypoint.sh && \
    echo 'mkdir -p /app/data' >> /usr/local/bin/entrypoint.sh && \
    echo 'chmod 777 /app/data' >> /usr/local/bin/entrypoint.sh && \
    echo 'chown -R $(id -u):$(id -g) /app/data 2>/dev/null || true' >> /usr/local/bin/entrypoint.sh && \
    echo '# Запускаем основное приложение' >> /usr/local/bin/entrypoint.sh && \
    echo 'exec "$@"' >> /usr/local/bin/entrypoint.sh && \
    chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Запускаем главный файл
CMD ["python", "svo-baggage-bot-fixed/bot.py"]
