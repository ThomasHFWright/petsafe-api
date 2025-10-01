# syntax=docker/dockerfile:1

FROM mcr.microsoft.com/vscode/devcontainers/python:3.11-bookworm

# Ensure pip is up to date and install the runtime dependencies needed by the
# integration tests.
RUN pip install --upgrade pip && \
    pip install httpx botocore pytest pytest-asyncio
