import win11toast
import time

def demo_advanced():
    # Advanced Entry Notification
    # Using multiple lines, clearer separation, and a button
    # If the user clicks the button, it can open the dashboard.
    
    body_entry = "Instrument: LONG NIFTY 25000 CE\nEntry Price: ₹105.50\nPosition Size: 75 Qty | Risk: ₹1,125"
    win11toast.toast(
        '🚀 TRADE EXECUTED', 
        body_entry, 
        button={'activationType': 'protocol', 'arguments': 'http://localhost:5000', 'content': 'View Dashboard'},
        audio={'src': 'ms-winsoundevent:Notification.IM'}
    )
    time.sleep(4)

    # Advanced Target Hit Notification
    body_target = "Instrument: NIFTY 25000 CE\nExit Price: ₹155.00\nTotal Profit: 🟩 +₹2,212.50"
    win11toast.toast(
        '💰 TARGET ACHIEVED', 
        body_target,
        button={'activationType': 'protocol', 'arguments': 'http://localhost:5000', 'content': 'Open Analytics'},
    )
    time.sleep(4)
    
    # Advanced Stop Hit Notification
    body_stop = "Instrument: BANKNIFTY 51000 PE\nExit Price: ₹200.00\nTotal Loss: 🟥 -₹1,500.00"
    win11toast.toast(
        '🛡️ STOPLOSS TRIGGERED', 
        body_stop,
        button={'activationType': 'protocol', 'arguments': 'http://localhost:5000', 'content': 'Review Trade'},
        audio={'src': 'ms-winsoundevent:Notification.Looping.Alarm', 'loop': 'false'}
    )
    time.sleep(4)

if __name__ == "__main__":
    demo_advanced()
