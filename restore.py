import codecs

with codecs.open(r"c:\Users\sagnik\Desktop\ut index 2\trading\trade_manager.py", "r", "utf-8") as f:
    lines = f.readlines()

new_load_state = """
    def load_state(self):
        \"\"\"Load state from SQLite on startup\"\"\"
        try:
            open_records = db.load_open_trades()
            closed_records = db.load_closed_trades()
            
            for data in open_records:
                tid = data.get("id", "")
                if not tid: continue
                t = Trade.from_dict(data)
                t.status = "OPEN"
                if t.entry_time.date() < datetime.now(IST).date():
                    t.execution_status = "RECOVERY_REQUIRED"
                    t.execution_error = "Previous-session trade requires broker reconciliation"
                    logger.critical(f"Previous-session active trade retained for reconciliation: {tid}")
                elif not t.execution_status:
                    t.execution_status = "OPEN"
                self.open_trades[tid] = t

"""

lines.insert(578, new_load_state)

with codecs.open(r"c:\Users\sagnik\Desktop\ut index 2\trading\trade_manager.py", "w", "utf-8") as f:
    f.writelines(lines)
