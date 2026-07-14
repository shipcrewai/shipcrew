# syntax=docker/dockerfile:1
FROM python:3.14-slim

WORKDIR /app/bots/shipply-doc-review

# Copy the bot package and install it. The bot depends on the pacto-bot-sdk
# package from PyPI, which is resolved automatically by pip.
COPY bots/shipply-doc-review/ ./
RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "shipply_doc_review"]
CMD []
