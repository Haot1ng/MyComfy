#!/bin/sh
# 在 engine 容器内按挂载的清单部署 ComfyUI。传入 --check 可仅校验清单和依赖锁。
set -eu

# 单文件部署使用过滤后的引擎配置，底层安装仍采用固定提交及带摘要的依赖锁。
if [ "${1:-}" = --recipe ]; then
    shift
    exec python -m mycomfy.engine.recipe --base /deployment --root "${MYCOMFY_DATA_ROOT:?}/engines" "$@"
fi
case "$#:${1:-}" in
    0:|1:--check) ;;
    *) printf '%s\n' '用法：mycomfy-deploy-engine [--recipe 文件或-] [--check]' >&2; exit 2 ;;
esac

manifest=/deployment/deployment.json
if [ ! -f "$manifest" ] || [ ! -r "$manifest" ]; then
    printf '无法读取 %s，请检查部署目录的挂载和清单权限。\n' "$manifest" >&2
    exit 1
fi

# 安装目录与运行管理读取的 engines.json 共用同一持久化卷。
exec python -m mycomfy.cli deploy "$manifest" --root "${MYCOMFY_DATA_ROOT:?}/engines" "$@"
