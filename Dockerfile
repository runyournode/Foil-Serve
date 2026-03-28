# Build offline image, inspired from <https://github.com/PaddlePaddle/PaddleOCR/blob/4fe36883/deploy/paddleocr_vl_docker/accelerators/nvidia-gpu/pipeline.Dockerfile#L36-L55>
# Build this image (match tag version with pyproject.toml):
# `docker build -t ghcr.io/runyournode/foil-serve:<tag> .`
# `docker build -t ghcr.io/runyournode/foil-serve:0.1.7-oom.debug .`


# Build venv
FROM python:3.13.12-slim-trixie AS uv_fetcher

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.10.9-python3.14-trixie-slim /usr/local/bin/uv /bin/uv
COPY pyproject.toml .python-version /app/
RUN echo "# placeholder" > README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv \
    && uv lock \
    && uv sync --no-dev

# Dowload models
FROM alpine:3.20 AS models_fetcher

ENV HOME=/home/foil

RUN apk add --no-cache wget tar

# Without PaddleOCR-VL-1.5
RUN --mount=type=cache,target=/model_cache \
        mkdir -p "${HOME}/.paddlex/official_models" \
        && cd "${HOME}/.paddlex/official_models" \
        && wget https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/UVDoc_infer.tar \
            https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-LCNet_x1_0_doc_ori_infer.tar \
            https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-DocLayoutV3_infer.tar \
        && tar -xf UVDoc_infer.tar \
        && mv UVDoc_infer UVDoc \
        && tar -xf PP-LCNet_x1_0_doc_ori_infer.tar \
        && mv PP-LCNet_x1_0_doc_ori_infer PP-LCNet_x1_0_doc_ori \
        && tar -xf PP-DocLayoutV3_infer.tar \
        && mv PP-DocLayoutV3_infer PP-DocLayoutV3 \
        && rm -f UVDoc_infer.tar PP-LCNet_x1_0_doc_ori_infer.tar PP-DocLayoutV3_infer.tar \
        && mkdir -p "${HOME}/.paddlex/fonts" \
        && wget -P "${HOME}/.paddlex/fonts" https://paddle-model-ecology.bj.bcebos.com/paddlex/PaddleX3.0/fonts/PingFang-SC-Regular.ttf

# Original full model download
#RUN --mount=type=cache,target=/model_cache if [ "${BUILD_FOR_OFFLINE}" = 'true' ]; then \
#        mkdir -p "${HOME}/.paddlex/official_models" \
#        && cd "${HOME}/.paddlex/official_models" \
#        && wget https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/UVDoc_infer.tar \
#            https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-LCNet_x1_0_doc_ori_infer.tar \
#            https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-DocLayoutV3_infer.tar \
#            https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PaddleOCR-VL-1.5_infer.tar \
#        && tar -xf UVDoc_infer.tar \
#        && mv UVDoc_infer UVDoc \
#        && tar -xf PP-LCNet_x1_0_doc_ori_infer.tar \
#        && mv PP-LCNet_x1_0_doc_ori_infer PP-LCNet_x1_0_doc_ori \
#        && tar -xf PP-DocLayoutV3_infer.tar \
#        && mv PP-DocLayoutV3_infer PP-DocLayoutV3 \
#        && tar -xf PaddleOCR-VL-1.5_infer.tar \
#        && mv PaddleOCR-VL-1.5_infer PaddleOCR-VL-1.5 \
#        && rm -f UVDoc_infer.tar PP-LCNet_x1_0_doc_ori_infer.tar PP-DocLayoutV3_infer.tar PaddleOCR-VL-1.5_infer.tar \
#        && mkdir -p "${HOME}/.paddlex/fonts" \
#        && wget -P "${HOME}/.paddlex/fonts" https://paddle-model-ecology.bj.bcebos.com/paddlex/PaddleX3.0/fonts/PingFang-SC-Regular.ttf; \
#    fi




# ---------------------------------
# Base: dependencies + venv + source (no models)
# ---------------------------------
FROM python:3.13.12-slim-trixie AS base

LABEL org.opencontainers.image.title="foil-serve"
LABEL org.opencontainers.image.description="FastAPI server for document-to-markdown conversion using PaddleOCR VL 1.5"
LABEL org.opencontainers.image.authors="runyournode"
LABEL org.opencontainers.image.licenses="Apache-2.0"

LABEL com.foil.cuda.version="13.0"
LABEL com.foil.engine="PaddlePaddle GPU 3.3.0"
LABEL com.foil.feature.libreoffice="true"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/home/foil
ENV PATH="/app/.venv/bin:${PATH}"

RUN groupadd -g 1000 foil \
    && useradd -m -s /bin/bash -u 1000 -g 1000 foil
WORKDIR /home/foil

# ---------------------------------
# Installation dépendances (libreoffice, python)
# ---------------------------------
RUN apt update \
    && apt upgrade -y \
    && apt install -y --no-install-recommends \
        libgl1    \
        libreoffice \
        libmagic1t64 \
        fontconfig \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-wqy-microhei \
        fonts-freefont-ttf \
    && apt autoremove -y \
    && apt clean \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# Default log location
RUN mkdir -p /var/log/foil && chown foil:foil /var/log/foil

# Venv and src
COPY --from=uv_fetcher --chown=foil /app/.venv /app/.venv
COPY --chown=foil src/foil_serve /app/

USER foil
WORKDIR /app

ENTRYPOINT ["uvicorn", "main:app"]
CMD ["--host", "0.0.0.0", "--port", "8080"]

ENV APP_PORT=8080
HEALTHCHECK --interval=120s --timeout=15s --start-period=60s \
  CMD python3 -c "import urllib.request, os; \
      port = os.getenv('APP_PORT'); \
      urllib.request.urlopen(f'http://localhost:{port}/health', timeout=15)" || exit 1


# ---------------------------------
# Light: no bundled models (downloaded at runtime via vLLM/PaddleOCR) (save ~ 200 MB compared to -offline)
# ---------------------------------
FROM base AS light
LABEL com.foil.feature.offline-ready="false"


# ---------------------------------
# Offline: models bundled in the image
# ---------------------------------
FROM base AS offline
LABEL com.foil.feature.offline-ready="true"
COPY --from=models_fetcher --chown=foil ${HOME}/.paddlex ${HOME}/.paddlex
