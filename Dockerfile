FROM python:3.11.16-slim-bookworm AS wheel-builder
WORKDIR /build
COPY requirements-build.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.lock
COPY pyproject.toml requirements.in README.md LICENSE THIRD_PARTY.md ./
COPY licenses ./licenses
COPY veil ./veil
RUN pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM python:3.11.16-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 VEIL_DATA=/data
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 fonts-dejavu-core fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY --from=wheel-builder /wheels /wheels
RUN pip install --no-cache-dir --no-deps /wheels/*.whl && rm -rf /wheels && pip uninstall --yes setuptools wheel pip && python -c 'from veil.engine import Engine; Engine()'
USER 1000:1000
CMD ["veil-api"]
