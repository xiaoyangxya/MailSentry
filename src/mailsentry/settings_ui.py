"""Task-oriented settings views for the single-Owner control panel."""

from mailsentry.config import ServiceConfig


def settings_context(panel, section, draft, *, values=None, error="", example=None):
    document, selected = panel.editing_document(draft)
    active = panel.config_store.active()
    config = selected.config if selected else ServiceConfig(accounts=())
    tests = panel.config_store.test_results(selected.id) if selected else frozenset()
    required = {f"account:{a.id}" for a in config.accounts if a.enabled}
    required.update(
        f"preview:{a.id}"
        for a in config.accounts
        if a.enabled and panel.storage.get_cursor(a.id) is None
    )
    if config.notifications.urls():
        required.add("notifications")
    if config.classification.cloud.enabled:
        required.add("cloud")
    return {
        "section": section,
        "selected_revision": selected.id if selected else "",
        "active_revision": active.id if active else "",
        "is_draft": bool(selected and (not active or selected.id != active.id)),
        "config": config,
        "accounts": config.accounts,
        "tests": tests,
        "ready": bool(selected and not required - tests),
        "remaining": len(required - tests),
        "config_text": selected.config_path.read_text(encoding="utf-8") if selected else "",
        "values": values or {},
        "error": error,
        "example": example,
        "timezone": document.get("timezone", "Asia/Shanghai"),
        "digest_times": ",".join(t.strftime("%H:%M") for t in config.notifications.digest_times),
    }
