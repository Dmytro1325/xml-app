# Використовуємо офіційний Python-образ
FROM python:3.12

# Встановлюємо робочу директорію всередині контейнера
WORKDIR /app

# Копіюємо всі файли у контейнер
COPY . /app

# Створюємо папку для збереження XML-файлів
RUN mkdir -p /app/output

# Переконуємось, що папка `templates/` існує
RUN mkdir -p /app/templates

# Переконуємось, що папка `SUCCESS_LOG_DIR` існує
RUN mkdir -p /app/SUCCESS_LOG_DIR


# Переконуємось, що папка `ERROR_LOG_DIR` існує
RUN mkdir -p /app/ERROR_LOG_DIR


# Встановлюємо залежності
RUN pip install --no-cache-dir -r requirements.txt

# Відкриваємо порт 8080
EXPOSE 8080

# Запускаємо FastAPI-додаток через Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]


