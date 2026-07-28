from windows_toasts import InteractableWindowsToaster, Toast

print("Sending test notification...")
try:
    toaster = InteractableWindowsToaster('Python')
    toast = Toast()
    toast.text_fields = ["UT Bot Diagnostics", "If you see this, the notification engine works!"]
    toaster.show_toast(toast)
    print("Notification sent successfully to Windows.")
except Exception as e:
    print(f"Failed to send notification: {e}")
