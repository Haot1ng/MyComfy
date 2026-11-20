#!/bin/sh
# 在 engine 容器内按挂载的清单部署 ComfyUI。传入 --check 可仅校验清单和依赖锁。
set -eu

case "$#:${1:-}" in
    0:|1:--check) ;;
    *) printf '%s\n' '用法：mycomfy-deploy-engine [--check]' >&2; exit 2 ;;
esac

manifest=/deployment/deployment.json
if [ ! -f "$manifest" ] || [ ! -r "$manifest" ]; then
    printf '无法读取 %s，请检查部署目录的挂载和清单权限。\n' "$manifest" >&2
    exit 1
fi

# 安装目录与运行管理读取的 engines.json 共用同一持久化卷。
exec python -m mycomfy.cli deploy "$manifest" --root "${MYCOMFY_DATA_ROOT:?}/engines" "$@"
