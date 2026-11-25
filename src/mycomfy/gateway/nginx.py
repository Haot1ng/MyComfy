"""把已校验的路由快照编译为 Nginx 原生共享内存限流规则。"""

import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT")
PREFIX = {"public": "/api/v1", "local": "/local/v1"}
# 未登记的 API 地址按监听器和 IP 共用一个桶，避免随机路径持续创建新桶。
FALLBACK = (120, 20)


def quote(value):
    """把静态值作为 Nginx 的单个字面量参数，禁止环境路径或路由夹带配置指令。"""
    value = str(value)
    if any(ch in value for ch in ('"', "'", "\\", "$", "\r", "\n", "\0")):
        raise ValueError("Unsupported character in Nginx argument")
    return '"' + value + '"'


def read_snapshot(path):
    """读取完整配置并核验摘要，仅接收可转换为有限路由模式的原生限流参数。"""
    with path.open("rb") as source:
        data = source.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Rules file is too large")
    snapshot = json.loads(data)
    routes, revision = snapshot["routes"], snapshot["revision"]
    canonical = json.dumps(
        routes, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if hashlib.sha256(canonical.encode()).hexdigest() != revision or not isinstance(routes, list):
        raise ValueError("Invalid rules revision")
    seen = set()
    for route in routes:
        listener, method, path = (route[k] for k in ("listener", "method", "path"))
        if (
            listener not in PREFIX
            or method not in METHODS
            or not path.startswith(PREFIX[listener] + "/")
        ):
            raise ValueError("Invalid rule scope")
        if not re.fullmatch(r"(?:/(?:[A-Za-z0-9_.-]+|\{[A-Za-z_][A-Za-z0-9_]*\}))+", path):
            raise ValueError("Invalid route template")
        if (listener, method, path) in seen:
            raise ValueError("Duplicate rule")
        seen.add((listener, method, path))
        for field in ("edge_rpm", "edge_burst"):
            val = route[field]
            if type(val) is not int or not 0 <= val <= 1_000_000:
                raise ValueError("Invalid rate or burst")
    return snapshot


def _listen(host, port):
    """生成安全的监听地址，同时支持 IPv4、IPv6 和已配置的主机名。"""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
            raise ValueError("Invalid listen host") from None
        return f"{host}:{port}"
    return f"[{address}]:{port}" if address.version == 6 else f"{address}:{port}"


def render(snapshot, shared, work):
    """编译双监听器、路径归一化、按来源限流、私有健康探针及脱敏访问计量。"""
    public_port = int(os.environ.get("MYCOMFY_API_PORT", "8000"))
    local_port = int(os.environ.get("MYCOMFY_LOCAL_PORT", "8001"))
    if not (0 < local_port < 65536 and 0 < public_port < 65536 and local_port != public_port):
        raise ValueError("Invalid listener ports")
    # Docker 把宿主端口转发到容器网卡；独立运行时管理入口仍只监听本机回环。
    if os.environ.get("MYCOMFY_GATEWAY_BRIDGE") == "1":
        public_host = admin_host = "0.0.0.0"
    else:
        public_host = os.environ.get("MYCOMFY_API_HOST", "127.0.0.1")
        admin_host = "127.0.0.1"
    socket_dir = Path(os.environ.get("MYCOMFY_CORE_SOCKET_DIR", "/run/mycomfy-api"))
    trusted = [
        ipaddress.ip_network(cidr.strip(), strict=False)
        for cidr in os.environ.get("MYCOMFY_TRUSTED_PROXY_CIDRS", "").split(",")
        if cidr.strip()
    ]

    # 固定路径优先，其次按每个路径段从左向右比较具体程度。参数仅匹配一段。
    def specificity(route):
        """让 /jobs/recent 与 /jobs/{id} 的匹配次序可预期。"""
        return tuple((part.startswith("{"), part) for part in route["path"].split("/"))

    routes = sorted(snapshot["routes"], key=specificity)
    groups = {FALLBACK}
    matches, policies = [], []
    for route in routes:
        method, path, listener = (route[k] for k in ("method", "path", "listener"))
        pattern = "/".join("[^/]+" if p.startswith("{") else re.escape(p) for p in path.split("/"))
        # 使用大小写敏感的正则，让网关与 FastAPI 的 URL 语义一致。
        matches.append(f'    "~^{listener}:{method}:{pattern}/?$" {quote(path)};')
        rate, burst = route["edge_rpm"], route["edge_burst"]
        name = f"g{rate}_{burst}" if rate else "off"
        policies.append(f"    {quote(f'{listener}:{method}:{path}')} {name};")
        if rate:
            groups.add((rate, burst))
    for listener, prefix in PREFIX.items():
        matches.append(f'    "~^{listener}:[A-Z]+:{prefix}(?:/|$)" {quote(prefix + "/*")};')
        policies.append(f'    "~^{listener}:[A-Z]+:{prefix}/[*]$" g{FALLBACK[0]}_{FALLBACK[1]};')

    blocks, limits, delays = [], [], []
    for rpm, burst in sorted(groups):
        group = f"g{rpm}_{burst}"
        # 同一组参数共用内存区，实际键仍包含完整路由及方法；未知路径统一归组。
        blocks.append(
            f'map $policy $key_{group} {{ default ""; {group} "$remote_addr:$port_role:$bucket_method:$route"; }}'
        )
        blocks.append(f"limit_req_zone $key_{group} zone={group}:1m rate={rpm}r/m;")
        limits.append(
            f"limit_req zone={group}" + (f" burst={burst} nodelay" if burst else "") + ";"
        )
        delays.append(f"{group} {(60 + rpm - 1) // rpm};")
    servers = []
    for listener, port, address in (
        ("public", public_port, public_host),
        ("local", local_port, admin_host),
    ):
        peer = socket_dir / ("public.sock" if listener == "public" else "admin.sock")
        trust = ""
        if trusted:
            trust = "real_ip_header X-Forwarded-For; real_ip_recursive on; " + " ".join(
                f"set_real_ip_from {network};" for network in trusted
            )
        # 管理端只传递匿名的转发标记，供 Core 拒绝回环 Host 降级；公开端只重建已核实的 XFF。
        servers.append(f"""
upstream core_{listener} {{ server {quote("unix:" + str(peer))}; }}
server {{
    listen {quote(_listen(address, port))};
    {trust}
    location / {{
        {" ".join(limits)}
        proxy_pass http://core_{listener};
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For {"$remote_addr" if listener == "public" else '""'};
        proxy_set_header X-Real-IP "";
        proxy_set_header X-Forwarded-Proto "";
        proxy_set_header X-Forwarded-Host "";
        proxy_set_header X-Forwarded-Port "";
        proxy_set_header Forwarded {"$admin_forwarded" if listener == "local" else '""'};
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 1h;
        proxy_intercept_errors off;
        proxy_next_upstream off;
    }}
    error_page 429 = @edge_rejected;
    location @edge_rejected {{
        default_type application/json;
        add_header Retry-After $retry_after always;
        add_header X-Request-ID $request_id always;
        add_header Cache-Control no-store always;
        return 429 '{{"error":{{"code":"EDGE_RATE_LIMIT","message":"Too many requests","details":[]}},"request_id":"$request_id"}}';
    }}
}}""")
    journal = shared / "journal"
    # 日志路径按完成分钟轮换。日记只含模板、方法、计数/时长所需字段。
    # $log_minute 来自经过严格格式匹配的系统时钟，不接受用户提供的文件名。
    log_dir = quote(journal).removesuffix('"')
    return f"""
worker_processes 2;
worker_shutdown_timeout 1h;
daemon off;
pid {quote(work / "nginx.pid")};
error_log stderr emerg;
events {{ worker_connections 2048; }}
http {{
    # 请求阶段可能包含原始 URI 和来源地址；只为主进程的启动故障保留 stderr。
    error_log /dev/null emerg;
    map_hash_bucket_size 256;
    root {quote(work)};
    client_body_temp_path {quote(work / "body")};
    proxy_temp_path {quote(work / "proxy")};
    fastcgi_temp_path {quote(work / "fastcgi")};
    uwsgi_temp_path {quote(work / "uwsgi")};
    scgi_temp_path {quote(work / "scgi")};
    client_max_body_size 0;
    server_tokens off;
    map "$http_forwarded$http_x_forwarded_for$http_x_forwarded_host$http_x_forwarded_proto$http_x_forwarded_port$http_x_real_ip" $admin_forwarded {{
        "" "";
        default "for=unknown";
    }}
    map $server_port $port_role {{ default ""; {public_port} public; {local_port} local; }}
    map $request_method $safe_method {{ default OTHER; {" ".join(f"{m} {m};" for m in METHODS)} }}
    map "$port_role:$safe_method:$uri" $route {{
        default "";
{chr(10).join(matches)}
    }}
    map "$port_role:$safe_method:$route" $policy {{
        default off;
{chr(10).join(policies)}
    }}
    map $route $bucket_method {{ default $safe_method; "~[*]$" ANY; }}
    map $route $log_api {{ "" 0; default 1; }}
    map $policy $retry_after {{ default 1; {" ".join(delays)} }}
    {chr(10).join(blocks)}
    limit_req_status 429;
    map $time_iso8601 $log_minute {{
        default unknown;
        "~^(?<datepart>[0-9-]+)T(?<hourpart>[0-9]+):(?<minpart>[0-9]+)" $datepart-$hourpart-$minpart;
    }}
    log_format traffic '$msec|$port_role|$safe_method|$route|$status|$limit_req_status|$request_time|$upstream_header_time';
    access_log {log_dir}/$log_minute.log" traffic if=$log_api;
    server {{ listen {quote("unix:" + str(work / "health.sock"))}; access_log off;
        location = /revision {{ return 200 "{snapshot["revision"]}"; }}
    }}
    {chr(10).join(servers)}
}}
"""
