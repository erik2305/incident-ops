FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml .
COPY src ./src
RUN python -m pip install --no-cache-dir .
CMD ["python", "-m", "incidentops.mcp.observability_server"]
