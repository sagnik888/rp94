with open('scanner.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    "self.invalidate_chart_caches()",
    "if hasattr(self, 'simulation_id'):\n            self.simulation_id += 1\n        self.invalidate_chart_caches()"
)

with open('scanner.py', 'w', encoding='utf-8') as f:
    f.write(content)
