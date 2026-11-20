#!/bin/sh
# 首次部署时从模板生成 .env，用当前宿主用户的身份访问挂载目录。
set -eu
cd "$(dirname "$0")/.."

case "$#:${1:-}" in
    0:) generate_passwords=no ;;
    1:--generate-passwords) generate_passwords=yes ;;
    *) printf '%s\n' '用法：sh scripts/init-env.sh [--generate-passwords]' >&2; exit 2 ;;
esac

if [ -f .env ]; then
    printf '%s\n' '已有 .env，将使用其中的配置。'
    exit 0
fi

# 将密码字段写入配置。自动模式只为尚未从环境提供的密码生成随机值。
write_password() {
    printf '%s=' "$1"
    if [ "$generate_passwords" = yes ] && [ -z "$2" ]; then
        # 将随机字节的十六进制表示直接写入临时配置文件。
        openssl rand -hex 32
    else
        printf '\n'
    fi
}

uid=$(id -u)
gid=$(id -g)
umask 077
draft=$(mktemp .env.XXXXXX)
trap 'rm -f -- "$draft"' EXIT
# 逐行保留模板注释和 Compose 表达式，只填入需要初始化的字段。
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        MYCOMFY_UID=*) printf 'MYCOMFY_UID=%s\n' "$uid" ;;
        MYCOMFY_GID=*) printf 'MYCOMFY_GID=%s\n' "$gid" ;;
        MYSQL_PASSWORD=) write_password MYSQL_PASSWORD "${MYSQL_PASSWORD:+set}" ;;
        REDIS_PASSWORD=) write_password REDIS_PASSWORD "${REDIS_PASSWORD:+set}" ;;
        *) printf '%s\n' "$line" ;;
    esac
done < .env.example > "$draft"

# 写完后才发布文件。即使两个终端同时初始化，也会保留先生成的配置。
if ln -T -- "$draft" .env; then
    printf '已生成 .env，UID=%s，GID=%s。\n' "$uid" "$gid"
    if [ "$generate_passwords" = no ]; then
        printf '%s\n' '请填写 MySQL 和 Redis 密码。'
    fi
else
    test -f .env
fi
