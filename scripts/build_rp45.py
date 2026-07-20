import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
import subprocess

RESTORE_POINT = 45
RP_NAME = f"restore_point_{RESTORE_POINT}"
SRC_DIR = Path(r"C:\Users\sagnik\Desktop\ut index 2")
DEST_DIR = SRC_DIR / RP_NAME

def build_rp45():
    print(f"Building {RP_NAME}...")
    EXCLUDE_DIRS = {".git"}
    src_resolved = SRC_DIR.resolve()
    dest_resolved = DEST_DIR.resolve()
    if dest_resolved.parent != src_resolved or dest_resolved.name != RP_NAME:
        raise RuntimeError(f"Unsafe restore-point destination: {dest_resolved}")
    
    if DEST_DIR.exists():
        shutil.rmtree(DEST_DIR)
    DEST_DIR.mkdir(parents=True)
    
    # 1. Copy files
    print("Copying files...")
    copied = 0
    for dirpath, dirnames, filenames in os.walk(SRC_DIR):
        rel_dir = Path(dirpath).relative_to(SRC_DIR)
        
        # Filter dirnames
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.lower().startswith("restore_point_") and not d.lower().startswith("restorepoint_")]
        
        for fname in filenames:
            if fname.startswith("RESTORE_POINT_") and (fname.endswith("_MANIFEST.json") or fname.endswith("_INFO.txt")):
                continue
                
            src_file = Path(dirpath) / fname
            target_file = DEST_DIR / rel_dir / fname
            target_file.parent.mkdir(parents=True, exist_ok=True)
            
            try:
                shutil.copy2(src_file, target_file)
                copied += 1
            except Exception as e:
                pass
                
    print(f"Copied {copied} files to {RP_NAME}")

    # 2. Write INFO txt
    info_text = f"""RESTORE POINT {RESTORE_POINT}
Updated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S +05:30")}
Source workspace: {SRC_DIR}
Restore target: {DEST_DIR}

Reason:
Restore point 45 built with full system updated and fixed (removed equity curve, expanded signal panel, fixed syntax errors, and clarified lot sizing logic).
"""
    info_path = SRC_DIR / f"RESTORE_POINT_{RESTORE_POINT}_INFO.txt"
    info_path.write_text(info_text, encoding="utf-8")
    shutil.copy2(info_path, DEST_DIR / info_path.name)
    
    # 3. Generate Manifest
    print("Generating Manifest...")
    # use fast_write_restorepoint_manifest.py since write_restorepoint_manifest.py might be the old one
    subprocess.run([sys.executable, "scripts/fast_write_restorepoint_manifest.py", "--out", f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json"], cwd=str(SRC_DIR))
    shutil.copy2(SRC_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json", DEST_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json")

    # 4. Update LATEST_RESTORE_POINT.txt
    latest_text = f"""Latest restore point: {RP_NAME}
Updated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S +05:30")}
Source workspace: {SRC_DIR}
Restore folder: {DEST_DIR}

Checkpoint summary:
- Built restore point {RESTORE_POINT} with full system updated and fixed (removed equity curve, expanded signal panel, updated start script branding).
"""
    (SRC_DIR / "LATEST_RESTORE_POINT.txt").write_text(latest_text, encoding="utf-8")
    shutil.copy2(SRC_DIR / "LATEST_RESTORE_POINT.txt", DEST_DIR / "LATEST_RESTORE_POINT.txt")

    print("Regenerating Manifest after latest marker update...")
    subprocess.run([sys.executable, "scripts/fast_write_restorepoint_manifest.py", "--out", f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json"], cwd=str(SRC_DIR), check=True)
    shutil.copy2(SRC_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json", DEST_DIR / f"RESTORE_POINT_{RESTORE_POINT}_MANIFEST.json")
    print(f"Restore Point {RESTORE_POINT} creation completed.")

if __name__ == "__main__":
    build_rp45()
