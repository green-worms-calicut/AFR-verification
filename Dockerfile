# The verifier service for gw_sales (verifier/) and the checks it runs (proof_check/). One image, run as the web
# receiver, the worker and the migrations: see deploy/docker-compose.yml.
#   docker build -t jev-verifier .
FROM python:3.14.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements-verifier.txt ./
RUN pip install --no-cache-dir -r requirements-verifier.txt
COPY proof_check/ proof_check/
COPY verifier/ verifier/
RUN useradd --system --uid 10001 verifier
USER verifier

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
CMD ["python", "-m", "verifier", "web", "--host", "0.0.0.0", "--port", "8080"]
