# Debian 12 部署指南

适用于 Debian 12（包括从 12.1 安装的系统）、使用 systemd 的服务器。目标仓库为 https://github.com/zhu961212/ykt_web ，默认分支为 main。本文按仓库脚本编写，尚未在你的远程服务器上实际执行部署。

## 1. 登录服务器，更新系统

在自己电脑的终端执行，把 SSH_USER、SERVER_IP 替换为实际登录用户和服务器 IP：

    ssh SSH_USER@SERVER_IP

如果不是 root 登录，执行：

    sudo -i

没有 sudo 时使用 su - 并输入 root 密码；已经是 root 则跳过。以下未特别注明的命令，都在服务器 root shell 执行：

    apt-get update
    apt-get upgrade -y
    apt-get install -y ca-certificates git python3 python3-pip python3-venv curl tar util-linux procps openssh-client
    python3 --version
    systemctl --version

项目要求 Python 3.10 或以上。不要为了保留“12.1”而停止安装 Debian 12 的安全更新。如果系统更新要求重启，先重启并重新登录，再继续。部署器会创建虚拟环境，无需向系统 Python 手动安装项目依赖。

## 2. 下载 GitHub 代码

    REPO='https://github.com/zhu961212/ykt_web.git'
    git ls-remote --heads "$REPO" main
    git clone --branch main "$REPO" /root/ykt_web
    cd /root/ykt_web

第一条 Git 查询应显示 refs/heads/main。如果仓库为私有且无法读取，先按文末“私有仓库”配置 SSH，再克隆。目标目录已存在时先确认内容，不要直接删除或覆盖。

**部署命令始终显式传入 --repo。** 脚本默认仓库也是 zhu961212/ykt_web，但以后若换成自己的 fork，只更改克隆地址还不够；--repo 必须改成同一目标地址，否则部署器仍可能拉取上游。

## 3. 选择一种访问方式

如果重新登录了服务器，请重新设置上一节的 REPO 变量，并进入 /root/ykt_web。

### A. 有域名：自动 HTTPS

先完成：

- 域名 A 记录指向服务器公网 IPv4，例如 panel.example.com。仅在服务器 IPv6 确实可达时保留 AAAA 记录。
- 云安全组和服务器防火墙放行 TCP 80、443，并保留 SSH 端口。不要开放后端 8765。
- 确认 80、443 没有被其他服务占用。已有 Nginx、Apache 或自行管理的 Caddy 时，按 README 的“已有反向代理”说明整合。
- 服务器能访问 GitHub、系统软件源、Python 包源和证书签发服务。

把下面域名替换为你的域名，执行：

    bash scripts/deploy.sh --repo "$REPO" --ref main --domain panel.example.com

--domain 只填域名，不带 https://、端口或路径。部署器安装/配置 Caddy，由 Caddy 监听公网 80/443、申请并续期证书，转发到非 root 应用服务的 127.0.0.1:8765，并启用安全 Cookie 和本机代理信任。软件源缺少 Caddy 时，脚本会核验官方签名密钥并添加 Caddy stable 源。不要把应用的 --port 设为 443。

完成后在自己电脑打开 https://panel.example.com/ 。

### B. 没有域名：使用 SSH 隧道

在服务器执行：

    bash scripts/deploy.sh --repo "$REPO" --ref main --host 127.0.0.1

此方式只监听服务器回环地址。无需开放 8765，也不必为本项目开放 80/443；保留 SSH 入口。

在**自己电脑另开终端**执行并保持窗口运行：

    ssh -N -o ExitOnForwardFailure=yes -L 18765:127.0.0.1:8765 SSH_USER@SERVER_IP

在自己电脑打开 http://127.0.0.1:18765/ 。本地使用 18765，避免和当前电脑已有的 8765 调试服务冲突：18765 是服务器版本，原来的 8765 页面仍是本地版本。

关闭隧道窗口后，服务器继续运行，但本机不能再通过该隧道访问。不要省略 --host 127.0.0.1：无域名模式默认监听 0.0.0.0:8765，允许远程直接通过未加密 HTTP 连接。

## 4. 登录面板并配置

部署器创建服务账号、虚拟环境和开机自启的 systemd 服务，并生成管理员账号（默认 admin）与强随机密码。服务器 root 查看：

    cat /opt/ykt-web/admin/service.env

在受信任的 SSH 终端查看 YKT_ADMIN_USERNAME、YKT_ADMIN_PASSWORD，登录后可以在“设置 → 管理员密码”修改。该文件仅 root 可读，更新不会覆盖；不要把内容发给他人或提交 Git。

新服务器下载的是源码，**不会带上当前电脑的真实配置、API Key、邮件凭据和雨课堂会话**。首次登录后：

1. 选择学校使用的雨课堂服务器。
2. 配置模型 API 地址、API Key 和模型，保存设置。
3. 重新扫码添加账号。
4. 先保留“自动答题并提交”和“登录后自动开始监课”关闭，检查连接与日志。
5. 在雨课堂 App 手动签到后，再按需要开始监课。

不要把本机真实的 config.json、session.json、日志、备份或环境文件上传到 GitHub。

### 保持进入课堂 5 秒，将提交延迟设为 15 秒

lesson.enter_delay_seconds 默认已是 5，检测到课堂后等待 5 秒，再检查手动签到并连接。bot.answer_delay_seconds 的示例默认仍是 5，所以服务器需要单独设成 15 才与当前电脑一致。

先在面板保存一次配置，让 /opt/ykt-web/app/config.json 存在。结束面板中的设置编辑后，在服务器 root 执行：

    systemctl stop ykt-web
    cp -p /opt/ykt-web/app/config.json "/opt/ykt-web/admin/config-before-delay-$(date +%Y%m%d-%H%M%S).json"
    python3 - <<'PY'
    import json
    from pathlib import Path

    path = Path('/opt/ykt-web/app/config.json')
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    config.setdefault('lesson', {})['enter_delay_seconds'] = 5
    bot = config.setdefault('bot', {})
    bot['answer_delay_seconds'] = 15
    bot['dry_run'] = True
    bot['auto_start_watching'] = False
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    PY
    chown ykt-web:ykt-web /opt/ykt-web/app/config.json
    chmod 600 /opt/ykt-web/app/config.json
    systemctl start ykt-web
    curl -fsS http://127.0.0.1:8765/api/health

复制代码时应使用 Markdown 渲染后的代码块，确保 Python 结束标记 PY 顶格。dry_run: true 表示只预览，不提交。15 秒相对题目出题时间计算，不是模型算完后再等 15 秒；实际提交还受截止时间、安全余量、模型耗时和面板开关约束。

## 5. 检查服务和日志

服务器上执行：

    systemctl status ykt-web --no-pager
    systemctl is-enabled ykt-web
    curl -fsS http://127.0.0.1:8765/api/health
    journalctl -u ykt-web -n 100 --no-pager

持续看日志使用 journalctl -u ykt-web -f，按 Ctrl+C 只会退出日志查看。管理服务使用：

    systemctl restart ykt-web
    systemctl stop ykt-web
    systemctl start ykt-web

有域名时再检查：

    getent ahosts panel.example.com
    systemctl status caddy --no-pager
    journalctl -u caddy -n 100 --no-pager
    ss -lntp
    curl -I https://panel.example.com/

- 后端 health 成功、HTTPS 失败：检查 DNS、不可达的 AAAA 记录、安全组/防火墙、80/443 端口占用和 Caddy 日志。
- 首次部署失败：修复日志指出的问题，在 /root/ykt_web 执行 git pull --ff-only，再运行同一条部署命令。首次成功前，admin/update.sh 可能还不存在；无需删除 /opt/ykt-web。
- 18765 无法访问：确认本机隧道仍运行且 SSH 连接成功，并检查服务器后端 health。

## 6. 后续更新

先在开发电脑测试并将代码推送到同一 GitHub 仓库的 main，然后在服务器 root 执行：

    /opt/ykt-web/admin/update.sh --check

看到 update-available 后：

    /opt/ykt-web/admin/update.sh
    systemctl status ykt-web --no-pager
    curl -fsS http://127.0.0.1:8765/api/health

更新器会沿用保存的仓库、分支和域名，在隔离目录安装依赖、执行编译和测试，再切换服务并检查健康状态。配置、登录会话、日志和管理员凭据会保留，启动或健康检查失败会尝试恢复上一版本。若检查显示 local-ahead 或 diverged，应先排查版本分叉，不要直接强推覆盖历史。

运行代码位于 /opt/ykt-web/app，部署 Git 仓库位于 /opt/ykt-web/repository，管理员脚本与凭据位于 /opt/ykt-web/admin。不要直接在运行目录 git pull。

## 附：仅在私有仓库时配置 Deploy key

本段在**服务器 root shell**执行，确保首次克隆和之后以 root 运行的部署器都能读取仓库。只给普通用户配置 SSH 不够。

1. 生成仅用于此部署的密钥；同名文件已存在时先检查，不要覆盖：

       install -d -m 700 /root/.ssh
       ssh-keygen -t ed25519 -f /root/.ssh/ykt-web-deploy -C 'ykt-web Debian deploy' -N ''
       chmod 600 /root/.ssh/ykt-web-deploy
       cat /root/.ssh/ykt-web-deploy.pub

2. 在 GitHub 目标仓库的 Settings → Deploy keys → Add deploy key 粘贴输出的公钥，不勾选 Allow write access。私钥只留在服务器。

3. 将以下段落加入 /root/.ssh/config；如已有同名 Host 段则修改它，避免重复：

       Host github-ykt-web
           HostName github.com
           User git
           IdentityFile /root/.ssh/ykt-web-deploy
           IdentitiesOnly yes

   然后执行：

       chmod 600 /root/.ssh/config
       ssh -T git@github-ykt-web

   首次连接对照 GitHub 官方 SSH 指纹核验后再确认。不要关闭主机密钥检查。成功提示中的“不提供 shell access”属于正常现象，命令可能返回退出码 1。

4. 使用 SSH 仓库地址：

       REPO='git@github-ykt-web:zhu961212/ykt_web.git'
       git ls-remote --heads "$REPO" main
       git clone --branch main "$REPO" /root/ykt_web
       cd /root/ykt_web

   随后回到第 3 节，继续使用此 REPO 变量执行部署。首次成功会保存 SSH 地址，后续更新继续使用 root 的专用密钥。不要把密码或 GitHub 令牌嵌入 URL。若报 Permission denied (publickey)，在 root 下重试 ssh -T 和 git ls-remote，核对 Deploy key 与 SSH 别名。

## 官方参考

- Debian 12 发行与维护信息：https://www.debian.org/releases/bookworm/
- Caddy 自动 HTTPS 与端口条件：https://caddyserver.com/docs/automatic-https
- GitHub Deploy keys：https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys
- GitHub SSH 指纹：https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints

项目参数和默认值以 scripts/deploy.sh、scripts/update.sh、config.example.json 为准。
