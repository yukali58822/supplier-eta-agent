FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY erpnext_app/supplier_eta/supplier_eta/setup/seed_data.json ./erpnext_app/supplier_eta/supplier_eta/setup/seed_data.json
RUN uv pip install --system --no-cache ".[anthropic,openai]"
COPY docker/agent-entrypoint.sh /usr/local/bin/agent-entrypoint.sh
RUN chmod +x /usr/local/bin/agent-entrypoint.sh && useradd -m agent
USER agent
ENV ETA_CONFIG_DIR=/app/config
EXPOSE 8000
ENTRYPOINT ["agent-entrypoint.sh"]
