with open('engine/alt_scanners.py', 'r', encoding='utf-8') as f:
    content = f.readlines()
for i, line in enumerate(content):
    if 'result["signals"] =' in line:
        print("".join(content[i-15:i+15]))
        break
