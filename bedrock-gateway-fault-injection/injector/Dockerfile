# Provider-agnostic Bedrock gateway fault injector.
# Small, single-process container. Runs anywhere a container runs:
# ECS/Fargate, EKS, EC2, or outside AWS. The only AWS dependency is read access
# to its own /bedrock-fis/{injector_id}/* SSM parameters (fail-open if absent).
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Drop to a non-root user.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin injector
USER 10001

EXPOSE 8080

# gunicorn with a modest worker/thread count; the injector is I/O bound on the
# upstream forward, so threads give cheap concurrency. Tune via env at deploy.
ENV GUNICORN_WORKERS=2 \
    GUNICORN_THREADS=8 \
    PORT=8080

# Healthcheck hits the never-faulted liveness endpoint.
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers ${GUNICORN_WORKERS} --threads ${GUNICORN_THREADS} --timeout 120 app:app"]
