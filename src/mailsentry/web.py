from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

from mailsentry.application import ControlPanel
from mailsentry.auth import AdminSession
from mailsentry.managed_config import ManagedConfigError
from mailsentry.settings_ui import settings_context

SESSION_COOKIE = "mailsentry_session"
PREAUTH_CSRF_COOKIE = "mailsentry_form_csrf"
MODULES = (
    ("dashboard", "/", "Dashboard", "\u603b\u89c8"),
    ("accounts", "/accounts", "Mailboxes", "邮箱管理"),
    ("rules", "/rules", "Detection rules", "检测规则"),
    ("ai", "/ai", "AI assessment", "AI 评估"),
    ("notifications", "/notifications", "Notifications", "通知设置"),
    ("review", "/configuration/review", "Test and apply", "测试并应用"),
    ("events", "/events", "Assessments", "\u8bc4\u4f30"),
    ("security", "/security", "Security", "\u5b89\u5168"),
    ("advanced", "/advanced", "Advanced", "高级设置"),
)


def localize_assessment_reason(reason: str, lang: str) -> str:
    if lang != "zh":
        return reason
    exact = {
        "baseline": "基础分",
        "sender is explicitly allowed": "发件人位于明确允许列表",
        "sender is explicitly denied": "发件人位于明确拒绝列表",
        "provider marked the message important": "模型供应商判定邮件重要",
        "message has a high-priority header": "邮件带有高优先级标头",
        "message has a potentially important attachment": "邮件包含可能重要的附件",
        "bulk-mail unsubscribe header": "邮件带有批量邮件退订标头",
        "negative sender feedback ignored for protected subject signal": (
            "受保护的主题信号生效，已忽略发件人的负面反馈"
        ),
    }
    if reason in exact:
        return exact[reason]
    prefixes = {
        "configured sender reputation: ": "已配置的发件人信誉：",
        "important subject keyword: ": "重要主题关键词：",
        "sender feedback adjustment: ": "发件人反馈调整：",
        "cloud: ": "云模型：",
    }
    for prefix, translated in prefixes.items():
        if reason.startswith(prefix):
            return translated + reason.removeprefix(prefix)
    return reason


def create_app(
    panel: ControlPanel,
    *,
    legacy_config: Path | None = None,
    legacy_env: Path | None = None,
    trust_proxy: bool = False,
    allow_insecure_http: bool = False,
) -> FastAPI:
    templates = Environment(
        loader=ChoiceLoader([
            FileSystemLoader(Path(__file__).with_name("templates")), DictLoader(TEMPLATES),
        ]),
        autoescape=select_autoescape(default=True),
    )
    templates.filters["assessment_reason"] = localize_assessment_reason
    pending_totp: dict[str, tuple[str, str]] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        panel.start()
        try:
            yield
        finally:
            panel.stop()

    app = FastAPI(
        title="MailSentry Control Panel",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(ManagedConfigError)
    async def managed_error(request: Request, exc: ManagedConfigError):
        return render(
            "message.html",
            request,
            session=session_for(request),
            message=localized_message(request, str(exc)),
            status=409,
        )

    def language(request: Request) -> str:
        selected = request.cookies.get("mailsentry_language")
        if selected in {"en", "zh"}:
            return selected
        accepted = request.headers.get("accept-language", "").lower()
        return "zh" if accepted.startswith("zh") else "en"

    def localized_message(request: Request, message: str) -> str:
        if language(request) != "zh":
            return message
        exact = {
            "invalid CSRF token": "CSRF 验证失败，请刷新页面后重试。",
            "configuration must be a YAML mapping": "配置必须是 YAML 映射结构。",
            "accounts must be a list": "邮箱账号必须使用列表格式。",
            "activate the initial configuration first": "请先激活初始配置。",
            "account id must be non-empty and unique": "账号 ID 不能为空且不能重复。",
            "mailbox username and password are required": "必须填写邮箱用户名和密码。",
            "custom IMAP accounts must be added in advanced settings": (
                "自定义 IMAP 账号必须在高级设置中添加。"
            ),
            "no active configuration": "当前没有已激活的配置。",
            "notification test failed": "通知测试失败。",
            "cloud classification is not enabled": "尚未启用云端评估。",
            "feedback has already been recorded for this event": (
                "此事件已经提交过反馈。"
            ),
            "invalid configuration revision": "配置版本无效。",
            "managed configuration already exists": "受管配置已经存在。",
        }
        return exact.get(message, f"操作失败：{message}")

    def render(
        name: str,
        request: Request,
        *,
        session: AdminSession | None = None,
        status: int = 200,
        **context: object,
    ) -> HTMLResponse:
        lang = language(request)
        preauth_csrf = request.cookies.get(PREAUTH_CSRF_COOKIE) or secrets.token_urlsafe(24)
        response = HTMLResponse(
            templates.get_template(name).render(
                request=request,
                lang=lang,
                t=TRANSLATIONS[lang],
                session=session,
                modules=MODULES,
                preauth_csrf=preauth_csrf,
                **context,
            ),
            status,
        )
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            PREAUTH_CSRF_COOKIE,
            preauth_csrf,
            httponly=True,
            secure=request.url.scheme == "https"
            or (
                trust_proxy
                and request.headers.get("x-forwarded-proto", "").lower() == "https"
            ),
            samesite="lax",
        )
        return response

    def session_for(request: Request) -> AdminSession | None:
        return panel.auth.authenticate(request.cookies.get(SESSION_COOKIE))

    def require_session(request: Request) -> AdminSession | RedirectResponse:
        session = session_for(request)
        return session if session is not None else RedirectResponse("/login", 303)

    def require_csrf(session: AdminSession, submitted: str) -> None:
        if not submitted or not secrets.compare_digest(session.csrf_token, submitted):
            raise ManagedConfigError("invalid CSRF token")

    def require_preauth_csrf(request: Request, submitted: str) -> None:
        expected = request.cookies.get(PREAUTH_CSRF_COOKIE, "")
        if not expected or not submitted or not secrets.compare_digest(expected, submitted):
            raise ManagedConfigError("invalid CSRF token")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        forwarded_https = (
            trust_proxy
            and request.headers.get("x-forwarded-proto", "").lower() == "https"
        )
        client_host = request.client.host if request.client else ""
        loopback = client_host in {"127.0.0.1", "::1", "testclient"}
        if (
            not allow_insecure_http
            and request.url.scheme != "https"
            and not forwarded_https
            and not loopback
        ):
            return PlainTextResponse("HTTPS is required for remote administration.", 400)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' https://unpkg.com; img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/healthz")
    def health() -> dict[str, object]:
        state = panel.runtime.status()
        return {
            "status": "ok" if state.error is None else "degraded",
            "runtime": state.running,
            "revision": state.revision,
        }

    @app.get("/language/{lang}")
    def set_language(lang: str, request: Request) -> RedirectResponse:
        response = RedirectResponse(request.headers.get("referer", "/"), 303)
        response.set_cookie(
            "mailsentry_language",
            lang if lang in {"en", "zh"} else "en",
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, token: str = "") -> HTMLResponse:
        return render("setup.html", request, token=token, error=None)

    @app.post("/setup", response_class=HTMLResponse)
    def complete_setup(
        request: Request,
        token: str = Form(),
        password: str = Form(),
        confirm_password: str = Form(),
        csrf_token: str = Form(),
    ):
        require_preauth_csrf(request, csrf_token)
        if password != confirm_password:
            return render(
                "setup.html",
                request,
                token=token,
                error="两次输入的密码不一致。" if language(request) == "zh"
                else "Passwords do not match.",
                status=400,
            )
        try:
            complete = panel.auth.complete_setup(token, password)
        except ValueError as exc:
            message = (
                "密码长度必须为 12 到 128 个字符。"
                if language(request) == "zh"
                else str(exc)
            )
            return render("setup.html", request, token=token, error=message, status=400)
        if not complete:
            return render(
                "setup.html",
                request,
                token="",
                error="设置令牌无效或已过期。" if language(request) == "zh"
                else "Setup token is invalid or expired.",
                status=400,
            )
        return RedirectResponse("/login", 303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        if panel.auth.needs_setup():
            message = TRANSLATIONS[language(request)]["setup_required"]
            return render("message.html", request, message=message)
        return render(
            "login.html",
            request,
            error=None,
            totp_enabled=panel.auth.totp_enabled(),
        )

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        password: str = Form(),
        totp_code: str = Form(default=""),
        csrf_token: str = Form(),
    ):
        require_preauth_csrf(request, csrf_token)
        remote = request.client.host if request.client else "unknown"
        session = panel.auth.login(password, totp_code, remote)
        if session is None:
            return render(
                "login.html",
                request,
                error="凭据无效或登录已被临时锁定。" if language(request) == "zh"
                else "Invalid credentials or login temporarily locked.",
                totp_enabled=panel.auth.totp_enabled(),
                status=401,
            )
        response = RedirectResponse("/", 303)
        secure = request.url.scheme == "https" or (
            trust_proxy and request.headers.get("x-forwarded-proto") == "https"
        )
        response.set_cookie(
            SESSION_COOKIE,
            session.token,
            httponly=True,
            secure=secure,
            samesite="lax",
            max_age=12 * 60 * 60,
        )
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form()):
        session = session_for(request)
        if session is not None:
            require_csrf(session, csrf_token)
            panel.auth.logout(session.token)
        response = RedirectResponse("/login", 303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        return render(
            "dashboard.html",
            request,
            session=session,
            dashboard=panel.dashboard(),
        )

    # Additional authenticated management routes are registered below.
    register_management_routes(
        app,
        panel,
        render,
        require_session,
        require_csrf,
        pending_totp,
        legacy_config,
        legacy_env,
        language,
        localized_message,
    )
    return app


def register_management_routes(
    app: FastAPI,
    panel: ControlPanel,
    render,
    require_session,
    require_csrf,
    pending_totp: dict[str, tuple[str, str]],
    legacy_config: Path | None,
    legacy_env: Path | None,
    language,
    localized_message,
) -> None:
    @app.get("/configuration", response_class=HTMLResponse)
    @app.get("/accounts", response_class=HTMLResponse)
    @app.get("/rules", response_class=HTMLResponse)
    @app.get("/ai", response_class=HTMLResponse)
    @app.get("/notifications", response_class=HTMLResponse)
    @app.get("/advanced", response_class=HTMLResponse)
    @app.get("/configuration/review", response_class=HTMLResponse)
    def configuration(request: Request, draft: str = ""):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        section = request.url.path.rsplit("/", 1)[-1]
        if section == "configuration":
            section = "accounts"
        if not draft:
            draft = request.cookies.get("mailsentry_draft", "")
            if draft:
                try:
                    panel.config_store.get(draft)
                except ManagedConfigError:
                    draft = ""
        context = settings_context(panel, section, draft)
        response = render(
            "configuration.html", request, session=session, **context,
            legacy_available=bool(
                not context["active_revision"] and legacy_config and legacy_config.exists()
            ),
        )
        if draft:
            response.set_cookie("mailsentry_draft", draft, httponly=True, samesite="lax")
        return response

    @app.post("/settings/{section}")
    async def save_settings(request: Request, section: str):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        values = {key: str(value) for key, value in (await request.form()).items()}
        require_csrf(session, values.get("csrf_token", ""))
        if section not in {"accounts", "rules", "ai", "notifications", "advanced"}:
            return PlainTextResponse("Unknown settings page", 404)
        source = values.get("source_revision", "")
        try:
            revision = panel.save_settings(
                section, values, source_revision=source,
                expected_revision=values.get("expected_revision") or None,
            )
        except ManagedConfigError as exc:
            safe_values = {k: v for k, v in values.items() if k not in {"password", "secret"}}
            return render(
                "configuration.html", request, session=session, status=400,
                **settings_context(panel, section, source, values=safe_values,
                                   error=localized_message(request, str(exc))),
            )
        response = RedirectResponse(f"/{section}?draft={revision.id}", 303)
        response.set_cookie("mailsentry_draft", revision.id, httponly=True, samesite="lax")
        return response

    @app.post("/settings/rules/example")
    async def rule_example(request: Request):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        values = {key: str(value) for key, value in (await request.form()).items()}
        require_csrf(session, values.get("csrf_token", ""))
        source = values.get("source_revision", "")
        example = panel.rule_example(source, values.get("sender", ""), values.get("subject", ""))
        return render(
            "configuration.html", request, session=session,
            **settings_context(panel, "rules", source, values=values, example=example),
        )

    @app.post("/configuration/stage")
    def stage_configuration(
        request: Request,
        csrf_token: str = Form(),
        expected_revision: str = Form(default=""),
        config_text: str = Form(),
        mailbox_secret_name: str = Form(default=""),
        mailbox_secret: str = Form(default=""),
        notification_secret_name: str = Form(default=""),
        notification_secret: str = Form(default=""),
        cloud_secret_name: str = Form(default=""),
        cloud_secret: str = Form(default=""),
        mailbox_secret_remove: str = Form(default=""),
        notification_secret_remove: str = Form(default=""),
        cloud_secret_remove: str = Form(default=""),
        mail_preset: str = Form(default="custom"),
        model_preset: str = Form(default="custom"),
        account_id: str = Form(default=""),
        account_username: str = Form(default=""),
        immediate_threshold: str = Form(default=""),
        digest_threshold: str = Form(default=""),
        digest_times: str = Form(default=""),
        retention_days: str = Form(default=""),
        cloud_enabled: str = Form(default="false"),
        cloud_model: str = Form(default=""),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        updates = {
            key: value
            for key, value in (
                (mailbox_secret_name, mailbox_secret),
                (notification_secret_name, notification_secret),
                (cloud_secret_name, cloud_secret),
            )
            if key and value
        }
        for key, remove in (
            (mailbox_secret_name, mailbox_secret_remove),
            (notification_secret_name, notification_secret_remove),
            (cloud_secret_name, cloud_secret_remove),
        ):
            if key and remove == "yes":
                updates[key] = None
        try:
            revision = panel.stage_with_presets(
                config_text,
                updates,
                expected_revision or None,
                mail_preset=mail_preset,
                model_preset=model_preset,
                account_id=account_id,
                account_username=account_username,
                immediate_threshold=immediate_threshold,
                digest_threshold=digest_threshold,
                digest_times=digest_times,
                retention_days=retention_days,
                cloud_enabled=cloud_enabled,
                cloud_model=cloud_model,
            )
        except ManagedConfigError as exc:
            return render(
                "message.html",
                request,
                session=session,
                message=localized_message(request, str(exc)),
                status=409,
            )
        return RedirectResponse(f"/configuration?draft={revision.id}", 303)

    @app.post("/configuration/accounts/add")
    def add_account(
        request: Request,
        csrf_token: str = Form(),
        account_id: str = Form(),
        username: str = Form(),
        password: str = Form(),
        preset: str = Form(default="gmail"),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        revision = panel.stage_new_account(
            account_id=account_id,
            username=username,
            password=password,
            preset=preset,
        )
        return RedirectResponse(f"/configuration?draft={revision.id}", 303)

    @app.post("/accounts/{account_id}/enabled")
    def set_account_enabled(
        account_id: str,
        request: Request,
        csrf_token: str = Form(),
        enabled: str = Form(),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        revision = panel.stage_account_enabled(account_id, enabled == "true")
        return RedirectResponse(f"/configuration?draft={revision.id}", 303)

    @app.post("/configuration/import")
    def import_legacy(request: Request, csrf_token: str = Form()):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        if legacy_config is None:
            return render(
                "message.html",
                request,
                session=session,
                message="没有可导入的旧版配置。"
                if language(request) == "zh"
                else "No legacy configuration.",
                status=404,
            )
        revision = panel.config_store.import_legacy(legacy_config, legacy_env)
        return RedirectResponse(f"/configuration?draft={revision.id}", 303)

    @app.post("/configuration/{revision_id}/test/account/{account_id}")
    def test_account(
        revision_id: str,
        account_id: str,
        request: Request,
        csrf_token: str = Form(),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        try:
            panel.test_account(revision_id, account_id)
        except Exception as exc:
            return render(
                "message.html",
                request,
                session=session,
                message=(
                    f"邮箱连接测试安全失败：{type(exc).__name__}"
                    if language(request) == "zh"
                    else f"Account test failed safely: {type(exc).__name__}"
                ),
                status=400,
            )
        return RedirectResponse(f"/configuration/review?draft={revision_id}", 303)

    @app.post("/configuration/{revision_id}/test/notifications")
    def test_notifications(
        revision_id: str,
        request: Request,
        csrf_token: str = Form(),
        confirm_send: str = Form(default=""),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        if confirm_send != "yes":
            message = (
                "必须明确确认发送真实测试通知。"
                if language(request) == "zh"
                else "Explicit send confirmation required."
            )
            return PlainTextResponse(message, 400)
        try:
            panel.test_notifications(revision_id, send=True)
        except Exception as exc:
            return render(
                "message.html",
                request,
                session=session,
                message=(
                    f"通知测试安全失败：{type(exc).__name__}"
                    if language(request) == "zh"
                    else f"Notification test failed safely: {type(exc).__name__}"
                ),
                status=400,
            )
        return RedirectResponse(f"/configuration/review?draft={revision_id}", 303)

    @app.post("/configuration/{revision_id}/preview/account/{account_id}")
    def preview_account(
        revision_id: str,
        account_id: str,
        request: Request,
        csrf_token: str = Form(),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        try:
            preview = panel.preview_account(revision_id, account_id)
        except Exception as exc:
            return render(
                "message.html",
                request,
                session=session,
                message=(
                    f"邮箱预览安全失败：{type(exc).__name__}"
                    if language(request) == "zh"
                    else f"Preview failed safely: {type(exc).__name__}"
                ),
                status=400,
            )
        return render(
            "preview.html",
            request,
            session=session,
            account_id=account_id,
            revision_id=revision_id,
            preview=preview,
        )

    @app.post("/configuration/{revision_id}/test/cloud")
    def test_cloud(
        revision_id: str,
        request: Request,
        csrf_token: str = Form(),
        confirm_cost: str = Form(default=""),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        if confirm_cost != "yes":
            message = (
                "必须明确确认模型请求可能产生费用。"
                if language(request) == "zh"
                else "Explicit provider-cost confirmation required."
            )
            return PlainTextResponse(message, 400)
        try:
            panel.test_cloud(revision_id)
        except Exception as exc:
            return render(
                "message.html",
                request,
                session=session,
                message=(
                    f"模型供应商测试安全失败：{type(exc).__name__}"
                    if language(request) == "zh"
                    else f"Provider test failed safely: {type(exc).__name__}"
                ),
                status=400,
            )
        return RedirectResponse(f"/configuration/review?draft={revision_id}", 303)

    @app.post("/configuration/{revision_id}/activate")
    def activate(
        revision_id: str,
        request: Request,
        csrf_token: str = Form(),
        expected_revision: str = Form(default=""),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        try:
            panel.activate(revision_id, expected_revision or None)
        except ManagedConfigError as exc:
            return render(
                "message.html",
                request,
                session=session,
                message=localized_message(request, str(exc)),
                status=409,
            )
        return RedirectResponse("/", 303)

    @app.get("/configuration/export")
    def export_configuration(request: Request):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        return PlainTextResponse(
            panel.config_store.export_redacted(),
            headers={
                "Content-Disposition": 'attachment; filename="mailsentry-redacted.yaml"'
            },
            media_type="application/yaml",
        )

    @app.get("/events", response_class=HTMLResponse)
    def events(request: Request, page: int = 1, level: str = ""):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        if level not in {"", "immediate", "digest", "audit"}:
            return PlainTextResponse(
                "提醒级别无效。" if language(request) == "zh"
                else "invalid alert level",
                400,
            )
        return render(
            "events.html",
            request,
            session=session,
            events=panel.events(page=page, level=level or None),
            page=max(page, 1),
            level=level,
        )

    @app.post("/events/{event_id}/feedback")
    def feedback(
        event_id: int,
        request: Request,
        csrf_token: str = Form(),
        verdict: str = Form(),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        if verdict not in {"important", "not-important"}:
            return PlainTextResponse(
                "反馈选项无效。" if language(request) == "zh" else "invalid verdict",
                400,
            )
        try:
            panel.feedback(event_id, verdict == "important")
        except ManagedConfigError as exc:
            return PlainTextResponse(localized_message(request, str(exc)), 409)
        return RedirectResponse("/events", 303)

    @app.post("/notifications/retry")
    def retry_notifications(request: Request, csrf_token: str = Form()):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        panel.retry_notifications()
        return RedirectResponse("/", 303)

    @app.get("/security", response_class=HTMLResponse)
    def security_page(request: Request):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        return render(
            "security.html",
            request,
            session=session,
            totp_enabled=panel.auth.totp_enabled(),
            totp=None,
            audit=panel.audit(),
            sessions=panel.auth.sessions(),
        )

    @app.post("/security/totp/begin", response_class=HTMLResponse)
    def begin_totp(request: Request, csrf_token: str = Form()):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        secret, uri = panel.auth.begin_totp()
        pending_totp[session.token] = (secret, uri)
        return render(
            "security.html",
            request,
            session=session,
            totp_enabled=False,
            totp={"secret": secret, "uri": uri},
            audit=panel.audit(),
            sessions=panel.auth.sessions(),
        )

    @app.post("/security/totp/confirm")
    def confirm_totp(
        request: Request,
        csrf_token: str = Form(),
        code: str = Form(),
    ):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        pending = pending_totp.pop(session.token, None)
        if pending is None or not panel.auth.enable_totp(pending[0], code):
            return PlainTextResponse(
                "TOTP 验证码无效。"
                if language(request) == "zh"
                else "Invalid TOTP code.",
                400,
            )
        panel.storage.add_audit("security.totp", "admin", "enabled")
        return RedirectResponse("/security", 303)

    @app.post("/security/totp/disable")
    def disable_totp(request: Request, csrf_token: str = Form()):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        panel.auth.disable_totp()
        panel.storage.add_audit("security.totp", "admin", "disabled")
        return RedirectResponse("/security", 303)

    @app.post("/security/sessions/revoke-others")
    def revoke_other_sessions(request: Request, csrf_token: str = Form()):
        session = require_session(request)
        if isinstance(session, RedirectResponse):
            return session
        require_csrf(session, csrf_token)
        count = panel.auth.revoke_other_sessions(session.token)
        panel.storage.add_audit(
            "security.sessions", "admin", f"revoked:{count}"
        )
        return RedirectResponse("/security", 303)


DEFAULT_CONFIG = """timezone: Asia/Shanghai
poll_seconds: 300
accounts:
  - id: personal
    connector: imap
    enabled: true
    host: imap.gmail.com
    port: 993
    username: you@gmail.com
    password_env: MAILSENTRY_ACCOUNT_PERSONAL_PASSWORD
    folder: INBOX
    tls: true
classification:
  immediate_threshold: 80
  digest_threshold: 50
  cloud:
    enabled: false
    base_url: https://api.deepseek.com
    model: deepseek-chat
    api_key_env: MAILSENTRY_CLOUD_API_KEY
    timeout_seconds: 15
notifications:
  urls_env: MAILSENTRY_APPRISE_URLS
  digest_times: ["09:00", "18:00"]
storage:
  path: data/mailsentry.db
  retention_days: 30
"""


TRANSLATIONS = {
    "en": {
        "product": "MailSentry",
        "dashboard": "Dashboard",
        "configuration": "Configuration",
        "events": "Assessments",
        "security": "Security",
        "logout": "Log out",
        "continue": "Continue",
        "create_admin": "Create administrator",
        "password": "Password",
        "confirm_password": "Confirm password",
        "admin_login": "Administrator login",
        "authenticator_code": "Authenticator code",
        "login": "Log in",
        "revision": "Revision",
        "not_configured": "Not configured",
        "runtime": "Runtime",
        "running": "Running",
        "stopped": "Stopped",
        "starting": "Starting",
        "retrying": "Retrying",
        "watching": "Watching",
        "pending_notifications": "Pending notifications",
        "failed_notifications": "Failed notifications",
        "monitored_accounts": "Monitored accounts",
        "account": "Account",
        "mailbox": "Mailbox",
        "status": "Status",
        "last_scan": "Last scan",
        "action": "Action",
        "enabled": "Enabled",
        "disabled": "Disabled",
        "not_bootstrapped": "Not bootstrapped",
        "enable": "Enable",
        "disable": "Disable",
        "retry_failed_notifications": "Retry failed notifications",
        "legacy_detected": "Legacy configuration detected",
        "legacy_import_help": (
            "Import creates a redacted managed draft. Original files are not "
            "overwritten."
        ),
        "import_legacy": "Import legacy configuration",
        "common_settings": "Common settings",
        "account_alias": "Account alias",
        "mailbox_address": "Mailbox address",
        "immediate_threshold": "Immediate threshold",
        "digest_threshold": "Digest threshold",
        "digest_times": "Digest times (comma separated)",
        "retention_days": "Retention days",
        "mailbox_preset": "Mailbox preset",
        "custom_imap": "Custom IMAP",
        "model_provider": "Model provider",
        "custom_responses": "Custom Responses endpoint",
        "cloud_assessment": "Cloud assessment",
        "model": "Model",
        "advanced_yaml": "Advanced YAML",
        "mailbox_secret_name": "Mailbox secret name",
        "replace_mailbox_secret": "Replace mailbox secret",
        "empty_keeps_current": "Empty keeps current value",
        "remove_mailbox_secret": "Remove mailbox secret",
        "notification_secret_name": "Notification secret name",
        "discord_apprise_url": "Discord webhook / Apprise URL",
        "remove_notification_secret": "Remove notification secret",
        "cloud_secret_name": "Cloud secret name",
        "replace_provider_key": "Replace provider key",
        "remove_provider_secret": "Remove provider secret",
        "validate_save_draft": "Validate and save draft",
        "add_gmail_account": "Add Gmail account",
        "gmail_address": "Gmail address",
        "application_password": "Application password",
        "add_as_draft": "Add as draft",
        "draft": "Draft",
        "integrations_must_pass": "Enabled integrations must pass before activation.",
        "test": "Test",
        "preview_24h": "Preview 24h",
        "send_notification_test": "Send notification test",
        "run_provider_test": "Run synthetic provider test",
        "activate_revision": "Activate tested revision",
        "export_redacted": "Export redacted configuration",
        "alert_level": "Alert level",
        "all": "All",
        "immediate": "Immediate",
        "digest": "Digest",
        "audit_only": "Audit only",
        "filter": "Filter",
        "time": "Time",
        "from_subject": "From / Subject",
        "assessment": "Assessment",
        "feedback": "Feedback",
        "recorded": "Recorded",
        "important": "Important",
        "not_important": "Not important",
        "next": "Next",
        "authenticator": "Authenticator",
        "totp_enabled": "TOTP enabled",
        "disable_totp": "Disable TOTP",
        "totp_help": (
            "Add this secret or URI to your authenticator, then confirm a code. "
            "This page is never cached."
        ),
        "code": "Code",
        "confirm_totp": "Confirm TOTP",
        "enable_totp": "Enable TOTP",
        "sessions": "Sessions",
        "created": "Created",
        "last_seen": "Last seen",
        "revoke_other_sessions": "Revoke other sessions",
        "administrative_audit": "Administrative audit",
        "target": "Target",
        "result": "Result",
        "preview_title": "24-hour preview",
        "preview_privacy": "No notification was sent. Message bodies were not persisted.",
        "from": "From",
        "subject": "Subject",
        "back_to_draft": "Back to draft",
        "setup_required": (
            "Administrator setup is required. Use the one-time URL printed by "
            "the service."
        ),
    },
    "zh": {
        "product": "\u4fe1\u54e8",
        "dashboard": "\u603b\u89c8",
        "configuration": "\u914d\u7f6e",
        "events": "\u8bc4\u4f30",
        "security": "\u5b89\u5168",
        "logout": "\u9000\u51fa",
        "continue": "继续",
        "create_admin": "创建管理员",
        "password": "密码",
        "confirm_password": "确认密码",
        "admin_login": "管理员登录",
        "authenticator_code": "身份验证器验证码",
        "login": "登录",
        "revision": "配置版本",
        "not_configured": "尚未配置",
        "runtime": "运行状态",
        "running": "运行中",
        "stopped": "已停止",
        "starting": "正在启动",
        "retrying": "正在重试",
        "watching": "正在监听",
        "pending_notifications": "待发送通知",
        "failed_notifications": "永久失败通知",
        "monitored_accounts": "监控邮箱",
        "account": "账号",
        "mailbox": "邮箱",
        "status": "状态",
        "last_scan": "最后扫描",
        "action": "操作",
        "enabled": "已启用",
        "disabled": "已停用",
        "not_bootstrapped": "尚未初始化",
        "enable": "启用",
        "disable": "停用",
        "retry_failed_notifications": "重试失败通知",
        "legacy_detected": "检测到旧版配置",
        "legacy_import_help": "导入会创建脱敏的受管草稿，不会覆盖原始文件。",
        "import_legacy": "导入旧版配置",
        "common_settings": "常用设置",
        "account_alias": "账号别名",
        "mailbox_address": "邮箱地址",
        "immediate_threshold": "即时通知阈值",
        "digest_threshold": "摘要通知阈值",
        "digest_times": "摘要时间（逗号分隔）",
        "retention_days": "保留天数",
        "mailbox_preset": "邮箱预设",
        "custom_imap": "自定义 IMAP",
        "model_provider": "模型供应商",
        "custom_responses": "自定义 Responses 兼容端点",
        "cloud_assessment": "云端评估",
        "model": "模型",
        "advanced_yaml": "高级 YAML",
        "mailbox_secret_name": "邮箱秘密变量名",
        "replace_mailbox_secret": "替换邮箱密码",
        "empty_keeps_current": "留空则保留当前值",
        "remove_mailbox_secret": "移除邮箱密码",
        "notification_secret_name": "通知秘密变量名",
        "discord_apprise_url": "Discord Webhook / Apprise URL",
        "remove_notification_secret": "移除通知秘密",
        "cloud_secret_name": "云模型秘密变量名",
        "replace_provider_key": "替换供应商密钥",
        "remove_provider_secret": "移除供应商密钥",
        "validate_save_draft": "校验并保存草稿",
        "add_gmail_account": "添加 Gmail 邮箱",
        "gmail_address": "Gmail 地址",
        "application_password": "应用专用密码",
        "add_as_draft": "添加到草稿",
        "draft": "草稿",
        "integrations_must_pass": "启用的集成必须通过测试后才能激活。",
        "test": "测试",
        "preview_24h": "预览最近 24 小时",
        "send_notification_test": "发送通知测试",
        "run_provider_test": "运行合成模型测试",
        "activate_revision": "激活已测试版本",
        "export_redacted": "导出脱敏配置",
        "alert_level": "提醒级别",
        "all": "全部",
        "immediate": "即时通知",
        "digest": "摘要通知",
        "audit_only": "仅记录",
        "filter": "筛选",
        "time": "时间",
        "from_subject": "发件人 / 主题",
        "assessment": "评估",
        "feedback": "反馈",
        "recorded": "已记录",
        "important": "重要",
        "not_important": "不重要",
        "next": "下一页",
        "authenticator": "身份验证器",
        "totp_enabled": "TOTP 已启用",
        "disable_totp": "停用 TOTP",
        "totp_help": "将此秘密或 URI 添加到身份验证器，然后输入验证码确认。本页不会被缓存。",
        "code": "验证码",
        "confirm_totp": "确认 TOTP",
        "enable_totp": "启用 TOTP",
        "sessions": "会话",
        "created": "创建时间",
        "last_seen": "最后活动",
        "revoke_other_sessions": "撤销其他会话",
        "administrative_audit": "管理审计",
        "target": "目标",
        "result": "结果",
        "preview_title": "24 小时预览",
        "preview_privacy": "未发送任何通知，也未持久化邮件正文。",
        "from": "发件人",
        "subject": "主题",
        "back_to_draft": "返回草稿",
        "setup_required": (
            "\u9700\u8981\u521d\u59cb\u5316\u7ba1\u7406\u5458\uff0c\u8bf7\u4f7f\u7528\u670d\u52a1\u65e5\u5fd7\u4e2d"
            "\u7684\u4e00\u6b21\u6027\u94fe\u63a5\u3002"
        ),
    },
}


BASE = """
<!doctype html><html lang="{{ lang }}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ t.product }}</title>
<script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"
 integrity="sha384-HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+"
 crossorigin="anonymous" defer></script>
<style>
:root{font-family:Inter,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
color:#243b33;background:#f5f7f6;font-size:15px;line-height:1.6}
*{box-sizing:border-box}body{margin:0}
.shell{display:grid;grid-template-columns:230px 1fr;min-height:100vh}
aside{background:#10291f;color:#fff;padding:24px}
aside a{display:block;color:#dff5e9;padding:10px;border-radius:8px;text-decoration:none}
main{padding:28px 42px 64px;max-width:1280px;width:100%;min-width:0}
aside{position:sticky;top:0;height:100vh}aside a{margin:4px 0}
aside a[aria-current="page"]{background:#254f3d;color:white;font-weight:700}
h1{font-size:18px;margin:0}h2{font-size:28px;margin:0}h3{font-size:18px;margin:0 0 12px}
p{margin:8px 0 16px}small{display:block;color:#61766c;font-size:13px;font-weight:400}
.top{border-bottom:1px solid #dce6df;padding-bottom:16px;margin-bottom:28px}
.page-heading,.card-heading,.test-row,.draft-bar{display:flex;align-items:center;
justify-content:space-between;gap:20px}.page-heading{margin-bottom:14px}
.eyebrow{font-size:11px;letter-spacing:.12em;color:#667e71;margin:0 0 4px}
.page-description{color:#61766c;margin-bottom:24px;max-width:760px}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px 24px}
.field{margin:0 0 16px}.field input,.field select,.field textarea{margin-top:6px}
.field small{margin-top:6px}.field:has(select){max-width:100%}
.form-footer{border-top:1px solid #e2e9e5;padding-top:16px;margin-top:20px;
display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.draft-bar{background:#e8f3ed;border:1px solid #b8d6c5;border-radius:12px;
padding:16px 20px;margin:0 0 24px}.notice{padding:16px;border-radius:10px;
background:#edf3f0;margin:16px 0}.notice.error{background:#fff0ef}
.check{display:flex;align-items:center;gap:10px;font-weight:400;margin:16px 0}
input[type=checkbox]{width:auto;padding:0;accent-color:#16794b}
textarea.list-input{min-height:110px;font-family:inherit;resize:vertical}
input,select,textarea,button{font:inherit}input:focus,textarea:focus,select:focus{
outline:2px solid #16794b;outline-offset:2px}button:disabled{opacity:.5;cursor:not-allowed}
button.secondary,.button.secondary{background:#edf3f0;color:#185e3e}
details{margin:12px 0}summary{cursor:pointer;color:#246345;font-weight:600;padding:8px 0}
.remove-mailbox summary{font-size:13px;color:#7a615e}.test-row{padding:14px 0;
border-bottom:1px solid #e2e9e5}.test-row:last-child{border:0}
.empty{text-align:center;padding:40px}.card-heading p{margin:0}
.card{background:#fff;border:1px solid #dce6df;border-radius:14px;padding:20px;
margin-bottom:18px;box-shadow:0 8px 24px #1635260c}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px}
.metric{font-size:1.8rem;font-weight:700}
label{display:block;font-weight:650;margin-top:12px}
input,textarea,select{width:100%;padding:10px;border:1px solid #b8c9bf;border-radius:8px}
textarea{min-height:360px;font-family:ui-monospace,monospace}
button,.button{display:inline-block;border:0;border-radius:8px;background:#16794b;
color:#fff;padding:10px 14px;text-decoration:none;cursor:pointer;margin:4px 4px 4px 0}
.danger{background:#a33}.muted{color:#607169}
.status{display:inline-block;padding:4px 9px;border-radius:999px;background:#e2f5e9}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px;border-bottom:1px solid #e2e9e5;vertical-align:top}
.top{display:flex;justify-content:space-between;align-items:center}
.inline{display:inline}.error{color:#a21d24}.secret{letter-spacing:.08em}
@media(max-width:760px){.shell{display:block}aside{padding:12px;display:flex;gap:4px;
overflow:auto;position:static;height:auto}aside h2{display:none}
aside a{white-space:nowrap}main{padding:16px}
.form-grid{grid-template-columns:1fr}.draft-bar,.test-row{align-items:stretch;flex-direction:column}
.page-heading{align-items:flex-start}h2{font-size:23px}
table{display:block;overflow:auto}}
</style></head><body hx-boost="true"><div class="shell">
{% if session %}<aside><h2>{{ t.product }}</h2>
{% for key,url,en,zh in modules %}
{% set settings_link = key in ['accounts','rules','ai','notifications','review','advanced'] %}
{% set suffix = '?draft=' ~ selected_revision if selected_revision and settings_link else '' %}
<a href="{{ url }}{{ suffix }}"
{% if section == key or request.url.path == url %}aria-current="page"{% endif %}>
{{ zh if lang == 'zh' else en }}</a>
{% endfor %}
</aside>{% endif %}
<main><div class="top"><h1>{{ t.product }}</h1><div>
<a href="/language/en">EN</a> / <a href="/language/zh">\u4e2d\u6587</a>
{% if session %}<form class="inline" method="post" action="/logout">
<input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
<button>{{ t.logout }}</button></form>{% endif %}</div></div>
{% block content %}{% endblock %}</main></div></body></html>
"""


TEMPLATES = {
    "base.html": BASE,
    "message.html": """
{% extends "base.html" %}{% block content %}
<div class="card"><p>{{ message }}</p><a class="button" href="/">{{ t.continue }}</a></div>
{% endblock %}
""",
    "setup.html": """
{% extends "base.html" %}{% block content %}
<div class="card"><h2>{{ t.create_admin }}</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post"><input type="hidden" name="token" value="{{ token }}">
<input type="hidden" name="csrf_token" value="{{ preauth_csrf }}">
<label>{{ t.password }}</label>
<input type="password" name="password" minlength="12" maxlength="128" required>
<label>{{ t.confirm_password }}</label>
<input type="password" name="confirm_password" required>
<button>{{ t.create_admin }}</button></form></div>{% endblock %}
""",
    "login.html": """
{% extends "base.html" %}{% block content %}
<div class="card"><h2>{{ t.admin_login }}</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post"><input type="hidden" name="csrf_token" value="{{ preauth_csrf }}">
<label>{{ t.password }}</label><input type="password" name="password" required>
{% if totp_enabled %}<label>{{ t.authenticator_code }}</label>
<input name="totp_code" inputmode="numeric" autocomplete="one-time-code" required>{% endif %}
<button>{{ t.login }}</button></form></div>{% endblock %}
""",
    "dashboard.html": """
{% extends "base.html" %}{% block content %}<h2>{{ t.dashboard }}</h2>
<div class="grid">
<div class="card"><span class="muted">{{ t.revision }}</span>
<div>{{ dashboard.revision or t.not_configured }}</div></div>
<div class="card"><span class="muted">{{ t.runtime }}</span>
<div class="metric">{{ t.running if dashboard.runtime.running else t.stopped }}</div></div>
<div class="card"><span class="muted">{{ t.pending_notifications }}</span>
<div class="metric">{{ dashboard.notifications_pending }}</div></div>
<div class="card"><span class="muted">{{ t.failed_notifications }}</span>
<div class="metric">{{ dashboard.notifications_failed }}</div></div>
</div><div class="card"><h3>{{ t.monitored_accounts }}</h3>
<table><tr><th>{{ t.account }}</th><th>{{ t.mailbox }}</th><th>{{ t.status }}</th>
<th>{{ t.last_scan }}</th><th>{{ t.action }}</th></tr>
{% for a in dashboard.accounts %}<tr><td>{{ a.id }}</td><td>{{ a.username }}</td>
<td><span class="status">{{ t.get(a.runtime.status, a.runtime.status)
or (t.enabled if a.enabled else t.disabled) }}</span>
{% if a.runtime.error %}<br><span class="error">{{ a.runtime.error }}</span>{% endif %}</td>
<td>{{ a.last_scan or t.not_bootstrapped }}</td><td>
<a class="button secondary" href="/accounts">
{{ '管理邮箱' if lang == 'zh' else 'Manage mailbox' }}</a></td></tr>
{% endfor %}</table></div>
<form method="post" action="/notifications/retry">
<input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
<button>{{ t.retry_failed_notifications }}</button></form>{% endblock %}
""",
    "events.html": """
{% extends "base.html" %}{% block content %}<h2>{{ t.events }}</h2>
<div class="card"><form method="get"><label>{{ t.alert_level }}</label>
<select name="level"><option value="">{{ t.all }}</option>
<option value="immediate">{{ t.immediate }}</option>
<option value="digest">{{ t.digest }}</option>
<option value="audit">{{ t.audit_only }}</option></select>
<button>{{ t.filter }}</button></form><table><tr><th>{{ t.time }}</th><th>{{ t.account }}</th>
<th>{{ t.from_subject }}</th><th>{{ t.assessment }}</th><th>{{ t.feedback }}</th></tr>
{% for e in events %}<tr><td>{{ e.received_at }}</td><td>{{ e.account_id }}</td>
<td>{{ e.sender }}<br><strong>{{ e.subject }}</strong></td>
<td>{{ e.score }} / {{ t.get(e.alert_level, e.alert_level) }}<br>
{% for reason in e.reasons %}{{ reason|assessment_reason(lang) }}
{% if not loop.last %}<br>{% endif %}{% endfor %}</td>
<td>{% if e.feedback_recorded %}<span class="status">{{ t.recorded }}</span>{% else %}
<form method="post" action="/events/{{ e.id }}/feedback">
<input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
<button name="verdict" value="important">{{ t.important }}</button>
<button class="danger" name="verdict" value="not-important">{{ t.not_important }}</button>
</form>{% endif %}</td></tr>{% endfor %}</table>
<a class="button" href="/events?page={{ page + 1 }}&level={{ level }}">{{ t.next }}</a>
</div>{% endblock %}
""",
    "security.html": """
{% extends "base.html" %}{% block content %}<h2>{{ t.security }}</h2>
<div class="card"><h3>{{ t.authenticator }}</h3>{% if totp_enabled %}
<p class="status">{{ t.totp_enabled }}</p><form method="post" action="/security/totp/disable">
<input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
<button class="danger">{{ t.disable_totp }}</button></form>{% elif totp %}
<p>{{ t.totp_help }}</p><code>{{ totp.secret }}</code><p><code>{{ totp.uri }}</code></p>
<form method="post" action="/security/totp/confirm">
<input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
<label>{{ t.code }}</label><input name="code" inputmode="numeric" required>
<button>{{ t.confirm_totp }}</button></form>{% else %}
<form method="post" action="/security/totp/begin">
<input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
<button>{{ t.enable_totp }}</button></form>{% endif %}</div>
<div class="card"><h3>{{ t.sessions }}</h3>
<table><tr><th>ID</th><th>{{ t.created }}</th><th>{{ t.last_seen }}</th></tr>
{% for item in sessions %}<tr><td>{{ item.id }}</td><td>{{ item.created_at }}</td>
<td>{{ item.last_seen_at }}</td></tr>{% endfor %}</table>
<form method="post" action="/security/sessions/revoke-others">
<input type="hidden" name="csrf_token" value="{{ session.csrf_token }}">
<button>{{ t.revoke_other_sessions }}</button></form></div>
<div class="card"><h3>{{ t.administrative_audit }}</h3>
<table><tr><th>{{ t.time }}</th><th>{{ t.action }}</th><th>{{ t.target }}</th>
<th>{{ t.result }}</th><th>{{ t.revision }}</th></tr>
{% for row in audit %}<tr><td>{{ row.created_at }}</td><td>{{ row.action }}</td>
<td>{{ row.target }}</td><td>{{ row.result }}</td><td>{{ row.revision or '' }}</td></tr>
{% endfor %}</table></div>{% endblock %}
""",
    "preview.html": """
{% extends "base.html" %}{% block content %}
<h2>{{ t.preview_title }}：{{ account_id }}</h2>
<p>{{ t.preview_privacy }}</p>
<div class="card"><table><tr><th>{{ t.time }}</th><th>{{ t.from }}</th><th>{{ t.subject }}</th>
<th>{{ t.assessment }}</th></tr>{% for item in preview %}<tr>
<td>{{ item.received_at }}</td><td>{{ item.sender }}</td><td>{{ item.subject }}</td>
<td>{{ item.score }} / {{ t.get(item.level, item.level) }}<br>
{% for reason in item.reasons %}{{ reason|assessment_reason(lang) }}
{% if not loop.last %}<br>{% endif %}{% endfor %}</td>
</tr>{% endfor %}</table>
<a class="button" href="/configuration/review?draft={{ revision_id }}">
{{ t.back_to_draft }}</a>
</div>{% endblock %}
""",
}
