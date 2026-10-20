FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY civconvert ./civconvert
COPY config ./config

RUN useradd --uid 10001 --no-create-home app
USER app

EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn civconvert.api:app --host 0.0.0.0 --port ${PORT}"]
