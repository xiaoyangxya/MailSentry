# 信哨 / MailSentry

[English](README.md)

MailSentry 是一个轻量的个人自托管服务。它以只读方式监控多个邮箱，对新邮件进行可解释评分，并在邮件可能重要时发送通知。它不会把邮件标记为已读、移动或删除邮件。

> 当前状态：基础版 v0.1。规则分类、SQLite 持久化、IMAP 轮询、Apprise 通知、CLI 和容器配置已经具备；IMAP IDLE、中等分数邮件摘要及可选云模型仍属于后续工作。

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
```

首次 `bootstrap` 会试跑最近 24 小时的邮件，但不会发送通知。之后 `run` 只处理已保存游标之后出现的新邮件。

## 配置与凭据

非敏感设置放在 `config.yaml`；邮箱密码和 Apprise URL 从配置指定的环境变量读取。
如需加载其他文件，可在命令名称前使用 `--env-file path\to\secrets.env`。不要提交 `.env`。

Apprise 可以连接 Telegram、ntfy、飞书、通用 Webhook 等通知渠道，具体格式参见 [Apprise 服务文档](https://appriseit.com/services/)。

## Docker

启动 Docker Desktop，创建 `config.yaml` 和 `.env`，然后运行：

```powershell
docker compose run --rm mailsentry bootstrap
docker compose up -d
```

邮件元数据存储在 `mailsentry-data` 数据卷中。邮件正文和附件内容不会持久化。

## 开发

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\ruff check .
```

MailSentry 使用 AGPL-3.0-or-later 许可证。
