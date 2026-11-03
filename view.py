with open('universal.pine', 'r', encoding='utf-8') as f:
    content = f.readlines()
for i, line in enumerate(content):
    if "SECTION 12" in line:
        print("".join(content[i:i+30]))
        break
