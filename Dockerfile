FROM python:3.12-slim

# Шрифты с поддержкой кириллицы (Liberation Sans — metric-compatible с Arial)
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-liberation \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# uv — быстрый менеджер пакетов
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Зависимости (отдельный слой для кэша)
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-editable

# Конфиг Streamlit
COPY .streamlit/ .streamlit/

# Приложение
COPY streamlit_app.py ./

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"

CMD ["uv", "run", "streamlit", "run", "streamlit_app.py", \
     "--server.address=0.0.0.0", "--server.port=8501"]
