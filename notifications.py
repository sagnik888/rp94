"""
Desktop Notifications — Windows Toast Notifications
═══════════════════════════════════════════════════════════════
"""

import threading
from loguru import logger

_toaster = None


def _get_toaster():
    global _toaster
    if _toaster is None:
        try:
            from windows_toasts import InteractableWindowsToaster
            _toaster = InteractableWindowsToaster('Command Prompt')
            logger.debug("Desktop notifications: windows_toasts ready")
        except ImportError:
            logger.warning("windows_toasts not installed — desktop notifications disabled")
    return _toaster


def send_desktop_notification(message: str, msg_type: str = "info"):
    """
    Send a Windows desktop toast notification.
    Runs in a separate thread to avoid blocking the event loop.
    """
    def _send():
        toaster = _get_toaster()
        if toaster is None:
            return
        try:
            from windows_toasts import Toast, ToastAudio, AudioSource
            
            titles = {
                "buy": "🟢 UT Bot — Trade Signal",
                "sell": "🔴 UT Bot — Trade Signal",
                "info": "📊 UT Bot — Info",
                "warning": "⚠️ UT Bot — Warning",
                "error": "❌ UT Bot — Error",
            }
            
            toast = Toast()
            title = titles.get(msg_type, "📊 UT Bot")
            toast.text_fields = [title, message[:256]]
            
            if msg_type in ("buy", "sell", "error", "warning"):
                toast.audio = ToastAudio(AudioSource.IM)
                
            toaster.show_toast(toast)
        except Exception as e:
            logger.debug(f"Notification error: {e}")

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
