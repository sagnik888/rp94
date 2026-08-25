"""
Desktop Notifications — Windows Toast Notifications
═══════════════════════════════════════════════════════════════
"""

import queue
import threading
from loguru import logger

_notification_queue: queue.Queue = queue.Queue(maxsize=100)
_worker_started = False
_worker_lock = threading.Lock()
_notifications_available = True


def _toast_event(kind: str):
    """A quiet callback for win11toast, whose defaults print to stdout."""

    def _handler(*args):
        logger.debug(f"Toast {kind}: {args}")

    return _handler


def _notification_worker():
    """Single background thread that processes all desktop notifications sequentially."""
    global _notifications_available
    try:
        import win11toast
    except Exception as exc:
        win11toast = None
        _notifications_available = False
        logger.warning(f"Desktop notifications unavailable: {exc}")

    titles = {
        "buy": "🟢 UT Bot — Trade Signal",
        "sell": "🔴 UT Bot — Trade Signal",
        "info": "📊 UT Bot — Info",
        "warning": "⚠️ UT Bot — Warning",
        "error": "❌ UT Bot — Error",
        "entry": "🚀 TRADE EXECUTED",
        "target": "💰 TARGET ACHIEVED",
        "stop": "🛡️ STOPLOSS TRIGGERED",
        "exit": "✅ TRADE EXITED",
    }

    while True:
        try:
            message, msg_type = _notification_queue.get()
        except Exception:
            continue

        try:
            if win11toast is None:
                continue
            title = titles.get(msg_type, "📊 UT Bot")

            button_config = None
            if msg_type in ["entry", "target", "stop", "exit"]:
                try:
                    from config.settings import get_settings

                    port = int(getattr(get_settings(), "dashboard_port", 8001) or 8001)
                except Exception:
                    port = 8001
                button_config = {
                    "activationType": "protocol",
                    "arguments": f"http://127.0.0.1:{port}",
                    "content": "View Dashboard",
                }

            # win11toast defaults on_click/on_dismissed/on_failed to the builtin
            # print(). Every toast that simply times out then writes
            # "(<ToastDismissalReason.TIMED_OUT: 2>,)" straight to STDOUT, which is
            # the launcher console. Route those callbacks to the log instead.
            kwargs = {
                "audio": {"silent": "true"},
                "on_click": _toast_event("click"),
                "on_dismissed": _toast_event("dismissed"),
                "on_failed": _toast_event("failed"),
            }
            if button_config:
                kwargs["button"] = button_config
            win11toast.toast(title, message[:256], **kwargs)
        except Exception as e:
            logger.debug(f"Notification error: {e}")
        finally:
            _notification_queue.task_done()


def send_desktop_notification(message: str, msg_type: str = "info"):
    """
    Send a Windows desktop toast notification.
    Enqueues to a single background worker to avoid thread-per-notification leak.
    """
    global _worker_started
    if not _worker_started:
        with _worker_lock:
            if not _worker_started:
                t = threading.Thread(target=_notification_worker, daemon=True, name="NotificationWorker")
                t.start()
                _worker_started = True
    try:
        _notification_queue.put_nowait((message, msg_type))
    except queue.Full:
        logger.debug("Notification queue full — dropping notification")
