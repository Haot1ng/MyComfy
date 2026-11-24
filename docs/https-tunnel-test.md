# 一条命令临时分享工作台和管理后台

业务机已运行 MyComfy 时，在仓库根目录的 WSL 或 Linux 终端执行下面的命令。

```bash
python3 scripts/deploy.py --test
```

脚本临时切换 Core 和网关的浏览器来源，启动两条 Tailscale Funnel 转发并打印**工作台**和**管理后台**两个 HTTPS 地址。保持这个终端打开。测试结束，在同一终端按 **Ctrl+C**，等脚本关闭本次开启的转发并恢复业务入口后再关闭终端。普通的单机、分机部署和停机命令继续使用各自的业务配置。

**工作台使用现有账号登录。管理链接允许免登录访问管理页面**，包括修改用户分组、发布模板和调整 API 限流。只把管理链接交给可以操作这套环境的人。测试连接的是已部署的数据库，测试期间提交的任务和后台修改会保留。

## 首次准备

1. 先完成业务部署并确认工作台和后台在本机能打开。单机 GPU 环境可用 `python3 scripts/deploy.py --single-host`。仅测试网页时，先运行 `python3 scripts/deploy.py --db-only`，再运行 `python3 scripts/deploy.py --core-only`。分机部署在运行 Core 和网关的业务机发起测试。[部署步骤](../README.md#启动服务)
2. 登录提供公网入口的 Tailscale 设备，并为该设备启用 Funnel 所需的 HTTPS 和访问权限。WSL 优先使用可用且已登录的 Windows `tailscale.exe`，也可使用 WSL 中已登录的 `tailscale`。原生 Linux 使用本机的 `tailscale`。客户端所在设备须能访问网关的两个本地端口，模板默认工作台为 `8000`、后台为 `8001`。[Tailscale Funnel 命令与授权](https://tailscale.com/kb/1311/tailscale-funnel)
3. 为工作台和后台各选一个空闲的 Funnel HTTPS 端口。可选值为 `443`、`8443` 和 `10000`，两个端口须不同。若端口已用于其他 Serve 或 Funnel 转发，选择另一个空闲端口。这两个数是浏览器使用的外部端口。转发目标使用已有业务部署的 `ports.api` 和 `ports.admin`。

如果以前从 Windows PowerShell 用 `--bg` 开过两条测试转发，先运行 `tailscale funnel status`。确认显示的端口和目标正是自己上次创建的测试规则后，可在 PowerShell 运行 `tailscale funnel --https=443 off` 和 `tailscale funnel --https=8443 off` 释放对应端口，再回到 WSL 执行 `--test`。端口按自己的旧规则填写。

## 测试配置

仓库提供带注释的 [deploy.test.example.yaml](../deploy.test.example.yaml)。默认设置使用当前 Tailscale 设备名、工作台 HTTPS 端口 `443` 和后台 HTTPS 端口 `8443`。首次省略 `--test-config` 且仓库还没有 `deploy.test.yaml` 时，脚本使用示例配置。两条转发都注册成功后，它会把检测到的完整设备名及两个端口写入本机的 `deploy.test.yaml`，供下次测试复用。Git 默认忽略这个文件。

也可以在首次测试前执行 `cp -n deploy.test.example.yaml deploy.test.yaml`，编辑副本后再启动。脚本保留已有配置和显式指定配置中的取值。想让配置跟随当前登录设备，可保留 `machine_name: auto`。三个字段都写在顶层。

```yaml
machine_name: auto       # 读取当前已登录设备的完整 DNS 名称
public_https_port: 443   # 分享给工作台用户
admin_https_port: 8443   # 分享给管理人员
```

`machine_name` 也可填写设备的完整名称，例如 `"studio.example.ts.net"`。显式名称必须与所用客户端报告的设备名称一致。使用自定义业务配置或测试配置时，可以分别指定文件。

```bash
python3 scripts/deploy.py --test --config ./deploy.prod.yaml --test-config ./share.qa.yaml
```

`--config` 指向**当初部署业务服务的完整配置**，包括数据库、路径、端口和常态浏览器来源。省略时沿用部署命令的默认查找顺序。`--test-config` 指向**本次测试使用的机器名和两个外部端口**。测试模式下，显式指定的文件必须存在。[部署配置的查找顺序](deploy-cli.md#配置文件与落盘位置)

## 打开与退出

- 将终端打印的工作台链接交给普通测试用户。测试期间，工作台端口临时绑定到本机回环地址，原局域网入口会暂停提供服务。切换时已打开的页面可能断线，可从终端打印的 HTTPS 地址重新进入。
- 按 **Ctrl+C** 结束前台测试。脚本先关闭本次开启的两个端口转发，再恢复这次部署原有的业务配置。收到恢复完成的提示后，使用原工作台或后台地址继续操作。
- 若启动时提示配置与运行中的服务不一致，用 `--config` 指向这套业务部署实际使用的文件后重试。若进程被强制终止或提示恢复失败，先确认 Docker 可用并检查这两个端口的转发状态，再用原配置和原部署模式恢复业务。例如原先单独部署业务端时，运行 `python3 scripts/deploy.py --config ./deploy.prod.yaml --core-only`，将文件名换成实际路径。完成后从原业务地址验证。
