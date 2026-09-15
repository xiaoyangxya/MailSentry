# 信哨 / MailSentry

[English](README.md)

MailSentry 是一个轻量的个人自托管服务。它以只读方式监控多个邮箱，对新邮件进行可解释评分，并在邮件可能重要时发送通知。它不会把邮件标记为已读、移动或删除邮件。

> 当前状态：基础版 v0.1。规则分类、SQLite 持久化、每账号独立 IMAP IDLE
> 监控与五分钟补偿扫描、定时摘要与即时 Apprise 通知、CLI 和容器配置已经具备；
> 同时已提供可选的 Responses 兼容云端评估适配器。

## 快速开始

需要 Python 3.11 或 Docker Desktop。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
```

编辑 `config.yaml` 和 `.env`，然后检查并启动。本地 Python 命令会自动读取
`.env`，但不会覆盖系统中已经设置的环境变量：

```powershell
.\.venv\Scripts\mail-sentinel doctor --no-connect
.\.venv\Scripts\mail-sentinel doctor
.\.venv\Scripts\mail-sentinel doctor --notify
.\.venv\Scripts\mail-sentinel bootstrap
.\.venv\Scripts\mail-sentinel run
.\.venv\Scripts\mail-sentinel status --connect
.\.venv\Scripts\mail-sentinel events --limit 20
.\.venv\Scripts\mail-sentinel explain --sender billing@example.com --subject "Invoice due"
```

首次 `bootstrap` 会试跑最近 24 小时的邮件，但不会发送通知。之后 `run` 只处理已保存游标之后出现的新邮件。

使用 `status` 查看各账号保存的扫描游标及通知投递计数；添加 `--connect`
会通过只读 IMAP 连接检查每个邮箱，任何连接失败都会令命令以失败状态退出。使用
`events` 以 JSON 行查看最近的可解释 Assessment，还可用 `--level immediate`、
`--level digest` 或 `--level audit` 过滤。

## 配置与凭据

非敏感设置放在 `config.yaml`；邮箱密码和 Apprise URL 从配置指定的环境变量读取。
如需加载其他文件，可在命令名称前使用 `--env-file path\to\secrets.env`。不要提交 `.env`。

个人 Gmail 与 Discord Webhook 的完整接入流程见
[Gmail 与 Discord 配置指南](docs/setup-gmail-discord.zh-CN.md)。

Apprise 可以连接 Telegram、ntfy、飞书、通用 Webhook 等通知渠道，具体格式参见 [Apprise 服务文档](https://appriseit.com/services/)。

摘要级别的 Assessment 会在配置时区的每个
`notifications.digest_times` 时刻合并为一条通知；默认使用
Asia/Shanghai 的 09:00 和 18:00。摘要时隙会持久化，程序重启后不会重复投递。

可解释规则评分会识别 Gmail 重要标记和常见高优先级邮件头，也会综合配置的
发件人、主题关键词、附件元数据、群发邮件头及已记录的 Feedback；含义相同的
重要标记只会加分一次。

可以通过 `classification.sender_reputation` 为精确发件人或 `@域名` 配置
-30 到 30 的调整值；精确发件人配置优先于域名配置。负面 Feedback 不会压低
明确的安全或付款主题信号。`explain` 会结合这些规则和已有发件人 Feedback
进行试算，但不会保存 Assessment 或发送通知；还可以添加
`--provider-important`、`--priority-header`、`--bulk` 或重复的
`--attachment` 参数来模拟不同信号。

### 可选云端 API 供应商

MailSentry 可以通过支持 OpenAI Responses 接口和 JSON Schema 结构化输出的供应商，
仅对本地规则判定为摘要级别的模糊邮件进行二次评估。`config.example.yaml` 以
DeepSeek 为例；其他兼容供应商只需替换 `base_url`、`model` 和 `api_key_env`：

```yaml
classification:
  cloud:
    enabled: true
    base_url: https://api.deepseek.com
    model: deepseek-flash
    api_key_env: MAILSENTRY_CLOUD_API_KEY
    timeout_seconds: 15
```

真实 Key 应写入 `.env`，例如 `MAILSENTRY_CLOUD_API_KEY=...`，不要直接写进 YAML。
可以用完全虚构的内容测试 API；该命令不会保存 Assessment 或发送通知：

```powershell
.\.venv\Scripts\mail-sentinel explain --sender test@example.com --subject "请审核" --text "这是一封虚构的测试邮件" --cloud
```

开启后，MailSentry 会以只读方式获取正文，但只向供应商发送发件人、主题、附件
元数据和最多 4,000 个清洗后的正文字符；账号别名、收件人、Message-ID、附件内容
和凭据不会发送。邮件字段始终按不可信输入处理，模型输出必须符合严格结构；超时
或输出无效时会安全退回本地规则 Assessment。`bootstrap` 也会用所配供应商评估
最近邮件中的模糊项；如果历史邮件不得离开 VPS，请保持此功能关闭。

## Docker

启动 Docker Desktop，创建 `config.yaml` 和 `.env`，然后运行：

```powershell
docker compose run --rm mailsentry bootstrap
docker compose up -d
```

服务器首次安装、一致性备份与恢复、凭据轮换、升级回滚、日志收集和事故处置，请按
[运维手册](docs/operations.md)执行。手册中的命令适用于 Linux VPS，且每一步都包含
完成检查。

邮件元数据存储在 `mailsentry-data` 数据卷中。邮件正文、临时云端摘要和附件内容
不会持久化；隐私回归测试还会确保临时内容、邮箱密码和通知 token 不会进入
SQLite、日志或默认通知文本。

## 开发

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
```

MailSentry 使用 AGPL-3.0-or-later 许可证。
