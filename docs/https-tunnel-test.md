# 用 HTTPS 链接测试工作台和管理后台

适用于 MyComfy 已在 WSL 2 和 Docker Desktop 中运行、测试人员从外网用浏览器打开页面的场景。Windows 宿主机运行 Tailscale Funnel。测试人员只需两个链接。

| 分享哪个链接 | 转发到业务机 | 测试人员能做什么 |
| --- | --- | --- |
| `https://desktop.tail1234.ts.net/` | `127.0.0.1:8000` | 使用工作台原有的注册和登录，按账号权限提交任务、查看实时进度和下载结果。 |
| `https://desktop.tail1234.ts.net:8443/` | `127.0.0.1:8001` | **无需登录**即可查看全局监控、创建和发布模板、查看全部账号并修改任意用户的权限组。 |

表中为示例网址，实际网址使用 Tailscale 给你分配的完整设备名。管理链接和本机管理地址提供相同的页面与操作。密码重置、分组定义等程序调用在两个入口都使用维护 Bearer 凭据。

## 窗口一 · 启动 WSL 和 MyComfy

先启动 Docker Desktop。在第一个 Windows 终端进入 Ubuntu。

```powershell
wsl -d Ubuntu-24.04
```

在 Ubuntu 提示符下更新项目。已经在运行的服务可以直接用于测试。

```bash
cd ~/MyComfy
git switch main
git pull --ff-only origin main
```

首次部署时运行 `python3 scripts/deploy.py --single-host`。如果是之前已经部署、后来停止的单机容器，用下面的命令启动已有服务。服务就绪后会在 Docker 中后台运行。

```bash
python3 scripts/deploy.py --compose --profile database up -d --no-recreate --no-build --wait mysql redis core engine worker
```

## 窗口二 · 同时开启两个 HTTPS 链接

第一次使用，在 **Windows** 安装 Tailscale 并从开始菜单打开、登录。主机操作者完成这一步后，测试人员只需浏览器。[Windows 下载](https://tailscale.com/download/windows)

```powershell
winget install --id Tailscale.Tailscale --exact
```

在 **Windows PowerShell** 执行下面这一行。示例对应 WSL 发行版 `Ubuntu-24.04` 和仓库 `/home/liu/MyComfy`。脚本根据自身所在的 WSL 路径识别发行版和仓库，读取 Docker Compose 实际使用的端口。`-ExecutionPolicy Bypass` 仅用于本次运行仓库内的脚本。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu-24.04\home\liu\MyComfy\scripts\share-test.ps1" -SingleHost
```

如果使用独立数据库并按分机模式部署，请在业务机运行并省略 `-SingleHost`。如果部署时用 `--config` 指定了文件，请在命令末尾加 `-Config "/在WSL中的路径/deploy.config.json"`。

首次运行可能会提示主机操作者打开 Tailscale 的网页启用 Funnel。如果授权页面提示需要重新运行，完成授权后再执行同一条命令。两条隧道就绪后，可以复制窗口里对应的网址。

- **Workbench** 是工作台，用户在这里注册或登录。
- **Admin** 是免登录后台，侧栏中的“用户管理”可以给任何用户改组。地址后追加 `users.html` 直达用户管理，追加 `templates.html` 直达模板页。

把需要的网址发给测试人员，保持第二个窗口运行。可用手机移动网络验证两个链接。[Funnel 使用条件](https://tailscale.com/docs/features/tailscale-funnel)

## 测试结束

在窗口二按 **Ctrl+C**，脚本会结束本次启动的两条隧道，并用启动时的部署配置恢复 `core`。只关闭隧道时，容器继续运行。若还要关闭 WSL，等隧道脚本退出后，在 Ubuntu 的项目目录运行 `python3 scripts/deploy.py --stop`，看到“所选服务已停止”再执行 `wsl --shutdown`。

如果 Windows 被强制关机，来不及执行恢复步骤，可在窗口一运行 `python3 scripts/deploy.py --single-host --test-tunnel off` 恢复原工作台地址。使用自定义部署文件时同样传入原来的 `--config`。
