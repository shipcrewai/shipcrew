# syntax=docker/dockerfile:1
FROM python:3.14-slim

WORKDIR /app/bots/shipply-gate-1

# Copy the bot package and install it. The bot depends on the pacto-bot-sdk
# package from PyPI, which is resolved automatically by pip.
COPY bots/shipply-gate-1/ ./
RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "shipply_gate_1"]
CMD []
