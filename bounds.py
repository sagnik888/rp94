with open('prompt.pine', 'r', encoding='utf-8') as f:
    lines = f.readlines()

def print_section_bounds():
    sections = {}
    current = None
    start = 0
    for i, line in enumerate(lines):
        if 'ENGINE' in line and ('UNIVERSAL' in line or 'APEX' in line or 'CARDWELL' in line or 'FORGE' in line):
            if current:
                sections[current] = (start, i)
            current = line.strip()
            start = i
    if current:
        sections[current] = (start, len(lines))
    
    for k, (s, e) in sections.items():
        print(f"{k}: {s} to {e} ({e-s} lines)")

print_section_bounds()
