FROM python:3.12-slim

# Logs sin búfer para Cloud Logging. BLAS con un hilo: el pronóstico ya reparte las series
# entre procesos y más hilos por proceso solo compiten por los mismos núcleos.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

WORKDIR /app

# Dependencias en una capa propia, que solo cambia cuando cambia pyproject.toml: así cada
# commit agrega a Artifact Registry una capa pequeña con el código y no otra copia de las
# dependencias.
COPY pyproject.toml README.md ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
extras = p['optional-dependencies']; \
print('\n'.join(p['dependencies'] + extras['gcs'] + extras['bigquery']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && rm -rf build \
    && useradd --create-home --uid 10001 pipeline

USER pipeline

# Cloud Run Job: inegi-market run (descarga y, si hay publicación pendiente, ciclo completo).
ENTRYPOINT ["inegi-market"]
CMD ["run"]
