# Використовуємо офіційний Python-образ
FROM python:3.12

# Встановлюємо робочу директорію всередині контейнера
WORKDIR /app

# Копіюємо всі файли у контейнер
COPY . /app

# Створюємо папку для збереження XML-файлів
RUN mkdir -p /app/output

# Встановлюємо залежності
RUN pip install --no-cache-dir -r requirements.txt

# Відкриваємо порт 8080
EXPOSE 8080

# Запускаємо FastAPI-додаток через Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
