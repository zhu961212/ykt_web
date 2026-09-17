# 雨课堂答题面板

一个可在本机或 Linux 服务器运行的雨课堂课堂监听与 LLM 辅助答题面板。它支持多账号扫码登录，在用户通过雨课堂 App 手动签到或在面板主动扫码签到后监听课堂事件，从课件中解析题目，调用 OpenAI 兼容模型生成答案，并可按题目截止时间选择是否自动提交。

> [!WARNING]
> 本项目不会后台自动签到；“扫码签到”只在用户主动提交二维码后执行。默认关闭自动提交。签到、自动答题或自动提交可能违反学校规定、课程要求或平台条款。请只在获得明确授权的测试、研究或个人学习场景中使用，并自行承担使用后果。

## 功能

- 多账号扫码登录、隔离会话与服务重启后恢复
- 同一课堂的多个账号共享解题结果，每道题只请求模型一次
- 独立 `/scan` 委托页面，使用专用扫码密码为全部已登录账号并发签到；不会后台自动签到
- 通过课堂 WebSocket 实时接收发题、延时、切换课件与下课事件
- 解析单选、多选、投票、填空、主观和判断题
- 支持 OpenAI 兼容的文字模型与视觉模型
- 根据真实截止时间、延时事件和安全边际安排提交
- 对模型 JSON、选项标识和填空数量进行校验
- 模型超时、图片读取失败或答案无效时不猜测、不提交
- 账号授权失效、连续监课异常和课堂连接异常邮件提醒
- 服务器管理员账号登录、远程访问、一键部署和安全更新检测
- 浏览器面板仅显示账号状态、配置和运行日志

## 架构

| 组件 | 职责 |
| --- | --- |
| `static/index.html` | 本地控制面板、扫码流程、模型配置与实时日志 |
| `server.py` | aiohttp Web 服务、多账号生命周期、课堂监听、截止时间与提交调度 |
| `ykt_core.py` | 雨课堂 HTTP 客户端、课件题目解析、图片处理、LLM 请求与答案校验 |
| `email_notifier.py` | SMTP 配置校验、异常通知、冷却去重和测试邮件 |
| `scripts/` | Linux systemd 一键部署、版本检测、更新和失败回滚 |
| `test_ykt_web.py` | 不访问雨课堂和模型服务的离线单元测试 |

核心流程如下：

```text
扫码登录 -> App 手动签到或面板扫码签到 -> 连接课堂 WebSocket -> 同步课件 -> 解析题目
         -> 调用 LLM -> 校验答案 -> 仅预览或在安全窗口内提交
```

每个账号拥有独立的 Cookie、课堂令牌、监听器、题目状态和提交时序；所有账号共享当前模型配置。同一雨课堂部署中，处于相同 `lessonId` 的账号会按题目 ID 复用同一份模型答案，各账号仍使用自己的凭据分别提交。不同课堂和不同题目不会共享答案。停止全部监课或退出全部账号时，共享解题任务和缓存会一并清理。

## 环境要求

- Python 3.10 或更高版本
- 可用的雨课堂账号，以及用于扫码登录和手动签到的雨课堂 App/微信
- 支持 `/models` 和 `/chat/completions` 的 OpenAI 兼容模型服务
- 图片题需要模型服务支持 `image_url` 多模态消息和 Data URI 图片

## 安装与运行

进入项目目录后创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
python server.py
```

在 Linux 或 macOS 上，将激活和复制命令替换为：

```bash
source .venv/bin/activate
cp config.example.json config.json
```

随后打开 <http://127.0.0.1:8765/>。本机手动运行默认监听回环地址；监听非本机地址时必须通过环境变量配置至少 12 位的管理员密码。

也可以不预先创建 `config.json`：程序会使用内置安全默认值启动，并在首次通过面板保存配置时写入该文件。复制示例文件主要用于提前查看和调整高级参数。

### Linux 服务器部署

有公网域名时，推荐让部署脚本同时配置 Caddy 和 HTTPS。开始前先完成以下准备：

- 将域名的 `A` 记录指向服务器公网 IPv4；只有服务器确实可通过 IPv6 访问时才保留 `AAAA` 记录
- 在云安全组和服务器防火墙中放行 TCP `80`、`443`
- 确认没有其他 Web 服务占用 `80`、`443`；已有 Nginx、Apache 或 Caddy 的服务器应改用下文的“已有反向代理”方式
- 确认服务器可以访问软件源、Caddy 官方签名仓库和公开证书签发服务

随后在支持 systemd 的 Linux 服务器上执行。`--domain` 只填写域名，不要包含 `https://`、端口或路径：

```bash
git clone https://github.com/zhu961212/ykt_web.git
cd ykt_web
sudo bash scripts/deploy.sh --domain panel.example.com
```

域名模式会安装并配置 Caddy；系统软件源没有 `caddy` 时，部署器会核对官方仓库签名密钥指纹并添加 Caddy stable 软件源。Caddy 监听公网 `80/443`、申请并自动续期证书，再将 HTTP 和 WebSocket 请求转发到 `127.0.0.1:8765`。Python 服务仍以非 root 账号运行，不会直接监听特权端口 `443`；部署器还会自动启用安全 Cookie 和本机反向代理信任。完成后访问 `https://panel.example.com/`，无需附加端口。首次签发证书可能需要短暂等待。

不要把应用的 `--port` 设置为 `443`。公网只需开放 `80/443`，不应开放后端端口 `8765`。手机浏览器的摄像头扫码要求 HTTPS，因此公网扫码页也应使用上述域名地址。

如果暂时没有域名，并且只在可信内网中使用，可以不传 `--domain`：

```bash
sudo bash scripts/deploy.sh
```

此模式默认监听 `0.0.0.0:8765`，通过 `http://服务器IP:8765` 访问，不提供传输加密，也不能满足手机浏览器的摄像头安全要求，不建议直接暴露到公网。

两种模式都会创建独立服务账号、虚拟环境和 systemd 服务，并生成管理员账号与强随机密码。首次凭据保存在仅 root 可读的 `/opt/ykt-web/admin/service.env`，更新不会覆盖。

#### 现有部署启用 HTTPS

已经使用旧版脚本部署的服务器，需要先完成一次普通更新，让服务器取得支持 `--domain` 的新版部署器，再配置域名：

```bash
sudo /opt/ykt-web/admin/update.sh
sudo /opt/ykt-web/admin/deploy.sh --domain panel.example.com
```

旧版部署器不认识 `--domain`，因此不要把域名参数加到第一次更新命令中。第二条命令只需配置现有版本，不要求使用 `--force`，并会保留账号、模型配置、登录状态和管理员凭据。完成迁移后，应从云安全组和服务器防火墙中关闭原先对公网开放的 `8765`。

需要停用安装器管理的 HTTPS 并恢复直接端口访问时执行：

```bash
sudo /opt/ykt-web/admin/deploy.sh --no-domain
```

该命令只移除本项目管理的 Caddy 站点，不会删除 Caddy 或改动其他站点；随后应按实际网络边界重新配置防火墙。

登录后可在“设置 -> 管理员密码”直接设置新密码，无需再次输入旧密码；为降低会话被盗风险，改密要求管理员在最近 10 分钟内登录。新密码需为 12-256 个可见字符，支持常见符号、空格和中文。密码使用随机盐 PBKDF2 哈希保存，修改后所有旧管理会话和 WebSocket 立即失效。`service.env` 中的密码仅作为首次部署和紧急恢复凭据。

忘记面板密码时，编辑环境文件，将 `YKT_ADMIN_RESET=1`，重启后用文件中的管理员账号密码登录并在设置页更新密码；随后把 `YKT_ADMIN_RESET` 改回 `0` 再重启：

```bash
sudoedit /opt/ykt-web/admin/service.env
sudo systemctl restart ykt-web
```

手动运行时也可以启用远程管理：

```bash
YKT_HOST=0.0.0.0 YKT_PORT=8765 \
YKT_ADMIN_USERNAME=admin YKT_ADMIN_PASSWORD='StrongPass_2026' \
python server.py
```

如果服务器已有统一管理的 Nginx、Caddy 或其他 HTTPS 入口，请不要让自动部署覆盖其站点配置。应让 ykt_web 只监听回环地址，并由现有代理转发请求；在 `service.env` 同时设置 `YKT_SECURE_COOKIE=1` 和 `YKT_TRUST_PROXY=1`，传递 `X-Forwarded-Proto`、`X-Forwarded-Host`，并让 `X-Forwarded-For` 的最后一项为代理实际看到的客户端 IP，然后重启服务。程序只信任来自本机回环地址的这些代理头。

#### HTTPS 排障

先确认 DNS、端口监听和两个服务的状态：

```bash
getent ahosts panel.example.com
sudo ss -lntp | grep -E ':(80|443|8765)\b'
sudo systemctl status ykt-web caddy --no-pager
sudo journalctl -u caddy -n 100 --no-pager
curl -fsS http://127.0.0.1:8765/api/health
curl -I https://panel.example.com/
```

后端健康检查成功但 HTTPS 仍失败时，通常是域名尚未解析到当前服务器、不可达的 `AAAA` 记录、云安全组或防火墙未放行 `80/443`，或者端口已被其他服务占用。证书签发依赖公网能够通过域名访问服务器，单纯把域名解析到 IP 并不会绕过这些条件。

首次部署如果在 Caddy 安装或 HTTPS 配置阶段失败，安装器会停止服务并保留失败候选。修复网络或更新安装器后，回到最初克隆的项目目录执行 `git pull`，再重新运行同一条 `sudo bash scripts/deploy.sh --domain ...` 命令即可；无需删除 `/opt/ykt-web`。首次部署成功前，`/opt/ykt-web/admin/deploy.sh` 可能尚不存在。

### 检查与安装更新

只检查远端是否有新版本，不修改运行中的服务：

```bash
sudo /opt/ykt-web/admin/update.sh --check
```

工具会区分 `up-to-date`、`update-available`、`local-ahead` 和 `diverged`，并显示当前与远端提交。确认有可快进更新后执行：

```bash
sudo /opt/ykt-web/admin/update.sh
```

候选版本会先在隔离目录安装依赖并运行编译和测试，随后才切换服务；`config.json`、`session.json`、`logs/` 和管理员凭据会保留。启动或健康检查失败时自动恢复上一版本，成功后只保留最近 1 个完整回滚版本。

通过 `--domain` 配置的域名会保存在部署元数据中。以后正常执行 `update.sh` 无需再次传入域名，更新后仍使用原来的 HTTPS 地址；证书申请和续期由 Caddy 独立管理。`--check` 只检查项目版本，不会重新配置代理或申请证书。

## 使用

1. 在“模型配置”中填写 API 地址和 API Key，点击“获取模型”，选择模型后保存。
2. 点击“添加账号”，使用微信扫码登录。
3. 在雨课堂 App 中手动签到，或点击面板“扫码签到”为全部账号提交课堂二维码。
4. 保持“自动答题并提交”关闭，先通过日志确认课程、题目解析和模型返回均正常。
5. 只有在你确认有权这样做时，才开启自动提交。

“登录后自动开始监课”默认关闭；开启后也只会启动课程检测，不会自动签到。可从面板单独启动、停止、检测或移除账号，也可以统一控制全部账号。

管理员先在“设置 -> 扫码页面”设置 4-128 个可见字符的独立扫码密码，再把 `/scan` 地址交给现场扫码人员。扫码角色只能提交签到，不能访问后台账号、状态、日志、WebSocket或配置；返回结果也不包含真实账号 ID 和姓名。修改扫码密码会立即让旧扫码会话失效。

“扫码签到”只接受雨课堂动态签到 URL（`https://*.yuketang.cn/api/v3/lesson/check-in/dynamic-qr-code...`）。系统会先为每个账号查询已加入的正在上课课程，再分别解析二维码；只有二维码 `lessonId` 在该账号课程列表中时才签到，并明确使用 `joinIfNotIn=false`，不会借签到自动加入课程。单个账号失败不会中断其他账号。进入扫码页后会自动打开默认后置摄像头，识别成功后直接提交且不显示二维码原文。浏览器提供能力时启用连续对焦；Safari 不开放网页强制对焦时由系统管理。原生 `BarcodeDetector` 不可用时会使用随项目提供的 `jsQR`。摄像头扫描需要 HTTPS 或 localhost。

### 模型配置

- API 地址可以是服务基础地址，也可以是完整的 `/chat/completions` 地址。
- 远程 API 必须使用 HTTPS；HTTP 只允许 `localhost`、`127.0.0.1` 或 `::1`。
- 同一 API 服务下，保存时将 Key 留空会保留现有值；切换 API 服务时必须填写对应的新 Key。
- API Key 写入本机 `config.json`，状态接口和网页不会回显其内容。
- 只有确认当前 API 和模型都支持图片输入后，才勾选“图片输入”。更换 API 地址或模型后应重新确认视觉能力。

### 邮件通知

在“邮件通知”中填写 SMTP 地址、端口、加密方式、用户名、授权码、发件地址和收件地址，保存后先发送测试邮件。仅支持 SSL/TLS 或 STARTTLS，不支持明文 SMTP。

SMTP 授权码只写入本机 `config.json`，接口与网页不会回显。修改 SMTP 主机、端口、加密方式或用户名时必须重新填写授权码。相同账号的同类故障按配置间隔去重，避免重连期间重复发信。

`config.example.json` 中的主要设置：

| 设置 | 说明 |
| --- | --- |
| `server` | 雨课堂部署，可选 `yuketang`、`pro`、`changjiang`、`huanghe` |
| `llm.base_url` | OpenAI 兼容 API 的基础地址或 Chat Completions 地址 |
| `llm.api_key` | 模型服务 API Key；不要提交真实值 |
| `llm.model` | 用于解题的模型 ID |
| `llm.vision_enabled` | 是否已确认模型支持图片输入，默认关闭 |
| `lesson.poll_interval` | 课程和手动签到状态的轮询间隔 |
| `email.enabled` | 是否发送账号与监课异常邮件 |
| `email.smtp_host` / `email.smtp_port` | SMTP 服务器地址与端口 |
| `email.security` | `ssl` 或 `starttls` |
| `email.password` | SMTP 授权码；不会通过接口回显 |
| `email.cooldown_seconds` | 相同故障邮件的最短间隔 |
| `bot.dry_run` | `true` 时只记录建议答案，不提交 |
| `bot.auto_start_watching` | 登录或恢复会话后是否自动开始监课 |
| `bot.answer_delay_seconds` | 相对出题时间的最短作答延迟 |
| `bot.safety_seconds` | 计划在真实截止时间前预留的安全边际 |
| `bot.request_margin_seconds` | 为提交 HTTP 请求额外预留的时间 |

## 文字题与图片题

雨课堂的课件页通常都带有 `cover`，因此项目不会仅凭 `cover` 判断为图片题。解析器会综合题干、选项和课件页内容：

- 题干和选项均有完整文字时，忽略通用课件封面并发送纯文本请求。
- 题干或选项直接包含图片、文字明确引用图表、文字内容缺失，或课件页存在显著视觉元素时，才附带相关图片。
- 题干兼容课件中的 `body`、`title`、`content` 和 `stem` 等字段。

来自受信任雨课堂 CDN 的图片会在本机下载、校验实际格式并转换为 Data URI 后发送给模型，避免模型服务无法访问课件 CDN。当前限制为最多 8 张图片、单张最多 4 MiB、合计最多 12 MiB；下载不跟随重定向。

如果视觉能力未确认、图片下载失败、模型返回错误或答案未通过结构校验，该题会被跳过。项目不会用固定选项兜底提交。

## 提交时序

每道题单独维护时间窗口：

```text
题目开始 = WebSocket 事件时间（异常或缺失时使用本机接收时间）
真实截止 = 题目开始 + 题目限时 + 累计延时
安全提交点 = 真实截止 - 有效安全边际
实际提交 = max(解题完成时间, 题目开始 + 最短作答延迟)
```

实际提交必须早于安全提交点。模型调用太慢、剩余时间不足或网络提交未能在窗口内完成时，系统会记录失败并停止该题流程。

## 测试

运行完整离线测试：

```bash
python -m unittest -v test_ykt_web.py test_email_notifier.py
```

测试使用模拟 HTTP、WebSocket、模型和时间事件，不需要真实账号、课堂或 API Key。GitHub Actions 会在受支持的 Python 版本上执行同一测试命令。

## 安全

- `config.json` 含 API Key，`session.json` 含登录 Cookie；两者只能保存在可信设备上，且不应提交到 Git。
- 远程管理必须设置 `YKT_ADMIN_USERNAME` 和至少 12 位的 `YKT_ADMIN_PASSWORD`；部署脚本会自动配置。
- 管理员登录能阻止未授权操作，但 HTTP 本身不加密；公网域名必须使用 HTTPS 反向代理。
- `config.json`、`session.json` 和 `/opt/ykt-web/admin/service.env` 应保持 `0600` 权限。
- 建议为模型服务使用权限受限、额度受限且可随时撤销的 Key。
- 如果凭据曾被提交或泄露，应立即撤销并重新生成；只从 Git 历史删除文件并不能使旧凭据失效。
- 安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 Issue 中披露利用细节或凭据。

## 已知限制

- 雨课堂使用非公开接口，平台升级后可能需要更新协议解析。
- 不同 OpenAI 兼容服务对模型列表、JSON 输出和图片消息的支持并不完全一致。
- 模型答案可能错误；启用自动提交前应先在仅预览模式验证。
- 本项目不保证适用于所有学校的私有化部署或所有题型变体。

## 来源与许可证

扫码会话迁移和课堂令牌处理流程参考了 [AneryCoft/course_helper](https://github.com/AneryCoft/course_helper)，课程发现与课件解析则结合当前客户端行为进行了适配。感谢相关公开源码项目提供的研究基础。

本项目采用 [GNU General Public License v3.0 only](LICENSE) 发布，以保持与参考项目的许可证兼容性。版权与第三方来源说明见 [NOTICE](NOTICE)；上游项目仍分别适用其各自许可证。

## 免责声明

本项目是非官方项目，与雨课堂、学堂在线及任何学校或课程方均无隶属、授权或背书关系。项目按“原样”提供，不保证可用性、准确性、兼容性或不会造成数据、账号、学业及其他损失。使用者有责任遵守适用法律、平台服务条款、学校规章、课程诚信要求和教师要求。
