FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md faultline.toml map.overrides.yaml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENTRYPOINT ["faultline"]
