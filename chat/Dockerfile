FROM python:3.11-slim

# Read CHAT_APP_PORT from .env at build time via --build-arg
# Usage: container build --build-arg CHAT_APP_PORT=5000 -t chat -f chat/Dockerfile .
ARG CHAT_APP_PORT=8001
ENV CHAT_APP_PORT=${CHAT_APP_PORT}

WORKDIR /chat

COPY chat/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Build from platform root: container build -t chat -f chat/Dockerfile .
COPY .env .
COPY chat/chat.py .
COPY chat/static/ ./static/

EXPOSE ${CHAT_APP_PORT}

CMD ["python", "chat.py"]
