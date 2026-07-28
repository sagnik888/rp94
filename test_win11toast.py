import win11toast
import sys

print("Sending win11toast...")
try:
    win11toast.toast('UT Bot', 'Testing win11toast notification engine')
    print("Sent successfully.")
except Exception as e:
    print(f"Error: {e}")
