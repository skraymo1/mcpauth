# Build context is the repository root (see azure.yaml), so the default test
# questions in foundry-project/ can ship with the app.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/entra-chat-demo

COPY entra-chat-demo/requirements.txt .
RUN pip install -r requirements.txt

COPY entra-chat-demo/config.py entra-chat-demo/chat.py entra-chat-demo/mcp_server.py entra-chat-demo/webapp.py entra-chat-demo/serve.py ./
COPY foundry-project/evals/datasets/aws-inventory-agent-turn-tests.jsonl /app/foundry-project/evals/datasets/

RUN useradd --system --uid 10001 --no-create-home app
USER 10001

ENV AUTH_MODE=entra \
    WEB_PORT=8000 \
    WEB_BIND_HOST=0.0.0.0

EXPOSE 8000
CMD ["python", "serve.py"]
