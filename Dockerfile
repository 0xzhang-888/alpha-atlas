FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src
COPY alpha-atlas/web /app/dashboard
COPY showcase /app/showcase

RUN mkdir -p /data
ENV DB_PATH=/data/messages.db

CMD ["python", "-m", "src.bot"]
