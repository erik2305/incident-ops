FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml .
COPY LICENSE .
COPY constraints.txt .
COPY src ./src
RUN python -m pip install --no-cache-dir -c constraints.txt .
CMD ["python", "-m", "incidentops.mcp.observability_server"]
