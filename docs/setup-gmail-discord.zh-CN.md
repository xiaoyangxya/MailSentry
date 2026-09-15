# Gmail 与 Discord 配置指南

[English](setup-gmail-discord.md)

本指南使用只读 IMAP 接入一个 Gmail Monitored Account，并把 MailSentry 通知发送到
Discord 私有频道。当前版本使用 Gmail 应用密码，尚未使用 Gmail OAuth。

## 1. 确认 Gmail 账号兼容

支持个人 Gmail；Google Workspace 账号则需要管理员同时允许 IMAP 和应用密码。

- 个人 Gmail 从 2025 年 1 月起始终启用 IMAP，界面里已经没有“启用 IMAP”开关，
  参见 [Gmail 客户端设置](https://support.google.com/mail/answer/7126229?hl=zh-Hans)。
- Workspace 管理员需要在 **管理控制台 → 应用 → Google Workspace → Gmail →
  最终用户访问权限 → POP 和 IMAP 访问权限**中允许 IMAP；策略最长可能需要 24
  小时生效。参见 [Workspace IMAP 管理](https://support.google.com/a/answer/105694?hl=zh-Hans)。
- 如果账号无法创建应用密码，当前 MailSentry 就无法连接。普通 Google 密码不能
  作为替代；Gmail OAuth/XOAUTH2 属于后续供应商能力。

账号已开启两步验证、可以打开“应用密码”页面，并且 Workspace 管理员确认允许
IMAP 后，兼容性检查完成。

## 2. 创建专用 Gmail 应用密码

1. 开启 [Google 两步验证](https://support.google.com/accounts/answer/185839?hl=zh-Hans)。
2. 打开 [Google 应用密码](https://myaccount.google.com/apppasswords)，将凭据命名为
   `MailSentry` 并生成。
3. 立即保存生成的 16 位密码，只用于这一套部署；不要填写 Google 账号主密码。

组织管理账号、高级保护计划或某些仅安全密钥的两步验证方式可能不显示应用密码。
限制条件见 [Google 应用密码说明](https://support.google.com/accounts/answer/185833?hl=zh-Hans)。

应用密码本身不是只读凭据，必须按账号秘密保护。只读保证来自 MailSentry：它只读
选择邮箱文件夹，不会标记、移动或删除源邮件。

## 3. 创建 Discord Webhook

建议使用专用私有文字频道；你的 Discord 角色需要 **管理 Webhook** 权限。

1. 打开 **服务器设置 → 整合 → Webhook**。
2. 选择 **新建 Webhook**，命名为 `MailSentry`，并选择目标频道。
3. 选择 **复制 Webhook URL**。Discord 官方流程见
   [Webhook 简介](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks)。

复制出的地址类似：

```text
https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN
```

取最后两段，转换成 MailSentry 使用的
[Apprise Discord](https://appriseit.com/services/discord/) 格式：

```text
discord://WEBHOOK_ID/WEBHOOK_TOKEN/
```

完整 Webhook URL 是持有者即可使用的秘密。不要添加 Apprise 的 `ping` 或 Markdown
格式参数；发件人、主题和 Assessment reasons 来自不可信邮件，不应触发 Discord 提及。

## 4. 配置 MailSentry

`config.yaml` 只写非秘密连接设置：

```yaml
accounts:
  - id: personal-gmail
    host: imap.gmail.com
    port: 993
    username: owner@gmail.com
    password_env: MAILSENTRY_GMAIL_APP_PASSWORD
    folder: INBOX
    tls: true

notifications:
  urls_env: MAILSENTRY_APPRISE_URLS
  digest_times:
    - "09:00"
    - "18:00"
```

Google 官方 [IMAP 协议说明](https://developers.google.com/workspace/gmail/imap/imap-smtp)
指定使用 `imap.gmail.com:993` 和 SSL/TLS。

两个秘密都放进 `.env`，并限制文件权限：

```dotenv
MAILSENTRY_GMAIL_APP_PASSWORD=替换为生成的应用密码
MAILSENTRY_APPRISE_URLS=discord://WEBHOOK_ID/WEBHOOK_TOKEN/
```

```bash
chmod 600 .env config.yaml
```

`MAILSENTRY_APPRISE_URLS` 可以用逗号分隔多个 Apprise 目的地；测试会向每个目的地发送。

## 5. 持续监控前验证

在 VPS 上依次验证本地配置、真实 Gmail 登录和 Discord 投递：

```bash
docker compose run --rm --no-deps mailsentry doctor --no-connect
docker compose run --rm --no-deps mailsentry doctor
docker compose run --rm --no-deps mailsentry doctor --notify
```

`--no-connect` 不能证明 Google 已接受凭据。第二条命令必须显示 Gmail 账号
`read_only=true`；第三条命令必须只向预期 Discord 频道发送一条测试消息。

三项都通过后再初始化游标并启动监控：

```bash
docker compose run --rm --no-deps mailsentry bootstrap
docker compose up -d --wait mailsentry
docker compose run --rm --no-deps mailsentry status --connect
```

容器健康、Gmail 游标已初始化、只读连接成功且 Discord 收到测试消息后，配置完成。

## 故障排查与轮换

- **找不到应用密码：** 检查两步验证、高级保护、仅安全密钥设置及 Workspace 策略。
  如果租户强制 OAuth，请等待 MailSentry 的 Gmail OAuth 供应商，不能改用主密码。
- **认证失败：** 检查完整 Gmail 地址，移除应用密码中意外的空格；若密码已撤销则
  重新生成。修改 Google 主密码会撤销已有应用密码。
- **Discord URL 无效：** 必须使用 `discord://ID/TOKEN/`，不能使用频道 URL、Bot
  Token 或已脱敏的 Webhook URL。
- **测试成功但频道没消息：** 检查 Webhook 是否仍指向预期频道，并检查全部 Apprise
  目的地是否可达。

计划轮换时先创建并验证新凭据，再撤销旧应用密码或删除旧 Webhook。怀疑泄漏时应
立即撤销或删除暴露的凭据，再部署新凭据。完整流程见[运维手册](operations.md)。
