FROM python:3.13-slim
WORKDIR /app
COPY services/requirements.txt .
COPY constraints.txt .
RUN python -m pip install --no-cache-dir -c constraints.txt -r requirements.txt
ARG SERVICE
COPY services/${SERVICE}/app.py ./app.py
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
