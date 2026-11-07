FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY packages ./packages
COPY services ./services
COPY knowledge ./knowledge
RUN pip install --no-cache-dir .

EXPOSE 8000
