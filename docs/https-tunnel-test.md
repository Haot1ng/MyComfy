# 用 HTTPS 链接测试工作台和管理后台

适用于 MyComfy 已在 WSL 2 和 Docker Desktop 中运行、测试人员从外网用浏览器打开页面的场景。Windows 宿主机运行 Tailscale Funnel。测试人员只需两个链接。

| 分享哪个链接 | 转发到业务机 | 测试人员能做什么 |
| --- | --- | --- |
| `https://desktop.tail1234.ts.net/` | `127.0.0.1:8000` | 使用工作台原有的注册和登录，按账号权限提交任务、查看实时进度和下载结果。 |
| `https://desktop.tail1234.ts.net:8443/` | `127.0.0.1:8001` | **无需登录**即可查看全局监控。打开“工作流模板”可预览、创建模板并发布版本。拿到链接的任何人都有这些能力。 |

表中为示例网址，实际网址使用 Tailscale 给你分配的完整设备名。管理后台的用户、分组和密码重置维护接口只供本机调用。两个隧道只转发网页端口。

## 1. 更新业务机并获取设备名称

在 **WSL 的 Ubuntu 终端**更新项目。

```bash
cd ~/MyComfy
git switch main
git pull --ff-only origin main
```

在 **Windows PowerShell** 安装 Tailscale，然后从开始菜单打开 Tailscale 并登录。只有运行隧道的主机需要这一步。[Windows 下载](https://tailscale.com/download/windows)

```powershell
winget install --id Tailscale.Tailscale --exact
```

重新打开 PowerShell，确认设备名并检查 Windows 可以访问两个本地页面。两条 HTTP 请求都应返回 200。若你在 MyComfy 中改过网页端口，请用实际端口检查。

```powershell
$deviceName = ((tailscale status --json | ConvertFrom-Json).Self.DNSName).TrimEnd('.')
$deviceName
curl.exe --noproxy "*" -I http://127.0.0.1:8000/
curl.exe --noproxy "*" -I http://127.0.0.1:8001/
```

## 2. 为已部署的 core 启用测试网址

在同一个 PowerShell 窗口执行下面的命令。这里按单机部署、发行版 `Ubuntu-24.04`、仓库 `/home/liu/MyComfy` 举例。分机部署时在业务机执行，去掉 `--single-host`。如果以前用 `--config` 指定了配置文件，开启和关闭测试时都传入那个文件。

```powershell
wsl -d Ubuntu-24.04 --cd /home/liu/MyComfy python3 scripts/deploy.py --single-host --test-tunnel $deviceName
```

脚本读取原部署配置，仅重新构建并启动 `core`。它会打印两个 HTTPS 网址，以及与实际业务端口匹配的两条 `tailscale funnel` 命令。此时工作台的注册、登录和提交任务使用打印的 HTTPS 来源。配置文件保留原值，MySQL、Redis、引擎和 Worker 可继续运行。

## 3. 启动隧道并分享链接

在**两个 Windows PowerShell 窗口**各运行脚本打印的一条命令。默认网页端口对应下列命令。

```powershell
# 窗口一，工作台
tailscale funnel --https=443 http://127.0.0.1:8000
```

```powershell
# 窗口二，管理后台
tailscale funnel --https=8443 http://127.0.0.1:8001
```

首次运行如果提示启用 Funnel，请由主机操作者打开终端给出的授权链接并完成设置。两个窗口显示公网地址后保持运行，用手机断开 Wi-Fi、通过移动网络打开链接即可验收。如果只测模板，可直接分享后台网址加 `/templates.html`。可在另一个 PowerShell 窗口用 `tailscale funnel status` 查看转发规则。[Funnel 使用条件](https://tailscale.com/docs/features/tailscale-funnel) · [命令说明](https://tailscale.com/docs/reference/tailscale-cli/funnel)

## 4. 测试结束

在两个运行隧道的窗口分别按 **Ctrl+C**，再在 PowerShell 恢复业务端原有的来源设置。

```powershell
wsl -d Ubuntu-24.04 --cd /home/liu/MyComfy python3 scripts/deploy.py --single-host --test-tunnel off
```

恢复后从原工作台地址登录。测试期间已经发布的模板和提交的任务保存在原数据库中。
