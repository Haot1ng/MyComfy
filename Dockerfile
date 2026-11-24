ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

FROM ${PYTHON_IMAGE} AS dependencies
WORKDIR /build
COPY pyproject.toml uv.lock ./
# 在使用企业代理的构建机，可用 ca_bundle 构建密钥临时提供受信任 CA。
RUN --mount=type=secret,id=ca_bundle,target=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt python -m pip install --no-cache-dir uv==0.13.6 \
    && UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --locked --no-dev --no-install-project \
        --system-certs --no-managed-python --python /usr/local/bin/python --no-cache

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MYCOMFY_DATA_ROOT=/var/lib/mycomfy
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 mycomfy \
    && useradd --uid 10001 --gid 10001 --create-home mycomfy \
    && install -d -o mycomfy -g mycomfy /var/lib/mycomfy
WORKDIR /opt/mycomfy
COPY --from=dependencies /opt/venv /opt/venv
COPY src/mycomfy/ ./mycomfy/
COPY migrations/ ./mycomfy/migrations/
# 兼容构建机的私有 umask，使服务用户能读取代码并进入包目录。
RUN chmod -R a+rX mycomfy
ENTRYPOINT ["python", "-m", "mycomfy.cli"]

FROM runtime AS worker
ENV MYCOMFY_WORKER_HEALTH_FILE=/tmp/mycomfy-worker.heartbeat
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-m", "mycomfy.generation.worker_health"]
USER mycomfy
CMD ["worker"]

FROM runtime AS engine
# Toolkit 注入宿主驱动的 CUDA/NVML 接口。deploy 按目标机的带哈希锁安装 PyTorch 和节点。
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dependencies /usr/local/bin/uv /usr/local/bin/uv
COPY --chmod=0755 scripts/deploy-engine.sh /usr/local/bin/mycomfy-deploy-engine
USER mycomfy
CMD ["runtime", "serve", "--stop-engines-on-exit"]

FROM ${PYTHON_IMAGE} AS gateway
# 网关控制程序只依赖 Python 标准库，入口限流由 Debian 的原生 Nginx 模块执行。
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 1777 /run/mycomfy-api \
    && install -d -o 10001 -g 10001 /var/lib/mycomfy-gateway
WORKDIR /opt/mycomfy
COPY src/mycomfy/gateway/ ./mycomfy/gateway/
RUN chmod -R a+rX mycomfy
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "mycomfy.gateway", "health"]
ENTRYPOINT ["python", "-m", "mycomfy.gateway"]

FROM runtime AS api
# StaticFiles 初始化时会校验目录。Compose 将网页只读挂载到这里。
RUN mkdir -p mycomfy/web/client mycomfy/web/admin && install -d -m 1777 /run/mycomfy-api
USER mycomfy
EXPOSE 8000
CMD ["api", "--host", "0.0.0.0"]
