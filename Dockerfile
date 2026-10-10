FROM python:3.11-slim

# Системные зависимости: tkinter для опционального GUI, curl для healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-tk \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем зависимости и ставим
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY app.py .

# Папки под тома Docker
RUN mkdir -p /app/data /app/notes /app/static

# Порты: 5000 (веб), 8000 (MCP sse)
EXPOSE 5000 8000

# ENV DOCKER=true отключает трей (в контейнере нет GUI)
ENV DOCKER=true
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -fs http://localhost:5000/api/stats || exit 1

# Без трея: только Flask + MCP в одном процессе
CMD ["python", "app.py", "--no-tray"]
