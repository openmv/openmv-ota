# Update-server image for self-hosters (and OpenMV's own hosted deploy).
# Multi-stage: build a wheel, then install it into a slim runtime with the cloud extras.
FROM python:3.12-slim AS build
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim
# server = fastapi/uvicorn/pydantic-settings/httpx; server-s3 = boto3 (R2/S3); server-postgres = psycopg.
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir "$(echo /tmp/*.whl)[server,server-s3,server-postgres]" \
    && rm -rf /tmp/*.whl
# Drop privileges; the app is stateless (artifacts in S3/R2, metadata in Postgres).
RUN useradd --create-home --uid 10001 app
USER app
# Render/Fly inject $PORT; server run binds it (falls back to 8080). `init` is idempotent.
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "openmv-ota server init && openmv-ota server run --host 0.0.0.0 --port ${PORT}"]
