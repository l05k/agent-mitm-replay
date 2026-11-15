FROM node:24.22.2-bookworm-slim

RUN npm install -g --no-audit --no-fund --fetch-retries=0 \
      @openai/codex@0.169.0 openclaw@2026.11.3 \
 && codex --version && openclaw --version

WORKDIR /app
