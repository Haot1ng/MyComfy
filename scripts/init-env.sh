#!/bin/sh
# 首次部署时从模板生成 .env，用当前宿主用户的身份访问挂载目录。
set -eu
cd "$(dirname "$0")/.."

if [ -f .env ]; then
    printf '%s\n' '已有 .env，将使用其中的配置。'
    exit 0
fi

# 只替换模板中的两个数字字段，保留密码填写位置和 Compose 的变量表达式。
uid=$(id -u)
gid=$(id -g)
umask 077
draft=$(mktemp .env.XXXXXX)
trap 'rm -f -- "$draft"' EXIT
sed -e "s/^MYCOMFY_UID=.*/MYCOMFY_UID=$uid/" \
    -e "s/^MYCOMFY_GID=.*/MYCOMFY_GID=$gid/" .env.example > "$draft"

# 写完后才发布文件。即使两个终端同时初始化，也会保留先生成的配置。
if ln -T -- "$draft" .env; then
    printf '已生成 .env，UID=%s，GID=%s。请填写 MySQL 和 Redis 密码。\n' "$uid" "$gid"
else
    test -f .env
fi
