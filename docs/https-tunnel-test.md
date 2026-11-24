# 临时外网测试（独立于部署）

此示例供已有 MyComfy 实例临时分享页面。Windows 上的 Tailscale 客户端独立管理转发；MyComfy 的一键部署只管理本机容器和监听端口。

## 窗口一 · WSL 中的业务服务

业务机已有 Nginx 两个入口，默认工作台为 `127.0.0.1:8000`，管理后台为 `127.0.0.1:8001`。若要在外部网页上登录和写入，浏览器实际使用的 HTTPS 来源须与部署配置一致。在 `deploy.config.yaml` 的 `public` 分组中更新下列字段，将两个示例地址换成自己设备的 HTTPS 来源。数据库、路径和端口沿用已有设置。

```yaml
public:
  host: 127.0.0.1
  origin: "https://desktop.example.ts.net"
  admin_origin: "https://desktop.example.ts.net:8443"
  cookie_secure: true
```

在 WSL 仓库目录运行 `python3 scripts/deploy.py --core-only` 应用业务配置。使用自定义配置文件时照常带上 `--config`。单机现场沿用本机数据库连接参数。上游转发保留浏览器发来的 Host，工作台和管理请求分别转发到各自的本地端口。已配置的管理 HTTPS 地址提供与本机相同的免登录管理操作，包括用户改组和修改限流规则。[反向代理与来源地址](api-traffic.md#反向代理和来源-ip)

## 窗口二 · Windows 上的外部工具

Windows 上已有登录的 Tailscale 客户端时，在 PowerShell 执行以下命令。先用 `tailscale funnel status` 确认这两个 HTTPS 端口空闲，再把两条转发分别指向实际业务端口。

```powershell
tailscale funnel --bg --https=443 http://127.0.0.1:8000
tailscale funnel --bg --https=8443 http://127.0.0.1:8001
tailscale funnel status
```

测试人员使用客户端输出的两个 HTTPS 链接。后台模式下关闭 PowerShell 窗口后转发仍会运行。测试结束，在 Windows 关闭本次增加的两条转发并核对状态。

```powershell
tailscale funnel --https=443 off
tailscale funnel --https=8443 off
tailscale funnel status
```

若临时修改过业务来源，在 WSL 还原原配置并执行 `python3 scripts/deploy.py --core-only`。外部工具的启停和 MyComfy 容器的启停可以分别操作。[Tailscale Funnel 命令说明](https://tailscale.com/kb/1311/tailscale-funnel)
