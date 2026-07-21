FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .
COPY . .
EXPOSE 3000
CMD ["dagster", "dev", "-m", "uncorrupt.pipelines.definitions", "-h", "0.0.0.0"]
