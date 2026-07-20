"""Script to mirror workspace files to restore point destination directories, excluding temp/dependency files."""

import os
import shutil
import sys
from pathlib import Path

# Target restore point name
RESTORE_POINT_NAME = "restore_point_28"

# Source directory is current workspace root
SRC_DIR = Path(".").resolve()

# Destination directories
DEST_DIRS = [
    Path(r"c:\Users\sagnik\Desktop\utbot al models") / RESTORE_POINT_NAME,
    Path(r"c:\Users\sagnik\Desktop\ut index 2") / RESTORE_POINT_NAME,
]

# Excluded directory names (exact matches)
EXCLUDE_DIRS = {
    ".git",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".benchmarks",
    "logs",
    "data_store",
    "archive",
    "ut1-index-final3",
}

def should_exclude_dir(dir_name: str) -> bool:
    if dir_name in EXCLUDE_DIRS:
        return True
    # Exclude other restore point directories
    name_lower = dir_name.lower()
    if name_lower.startswith("restore_point_") or name_lower.startswith("restorepoint"):
        return True
    return False

def should_exclude_file(file_name: str) -> bool:
    name_lower = file_name.lower()
    # Exclude temporary files and logs
    if name_lower.endswith(".log") or name_lower.endswith(".pyc") or name_lower.endswith(".pyo") or name_lower.endswith(".pyd"):
        return True
    # Exclude database files and backups
    if name_lower.endswith(".db") or ".db.bak" in name_lower or name_lower.endswith(".parquet"):
        return True
    # Exclude Fyers session token
    if file_name == "fyers_token.json":
        return True
    # Exclude manifests and info text files of OTHER restore points
    if file_name.startswith("RESTORE_POINT_") and (file_name.endswith("_MANIFEST.json") or file_name.endswith("_INFO.txt")):
        if "28" not in file_name:
            return True
    return False

def mirror():
    print(f"Starting mirror process from: {SRC_DIR}")
    
    # 1. Gather all files to copy
    files_to_copy = []
    
    for dirpath, dirnames, filenames in os.walk(SRC_DIR):
        # Resolve path objects relative to source
        rel_dir = Path(dirpath).relative_to(SRC_DIR)
        
        # Filter dirnames in-place to avoid traversing excluded directories
        dirnames[:] = [d for d in dirnames if not should_exclude_dir(d)]
        
        for fname in filenames:
            if should_exclude_file(fname):
                continue
            
            # Additional check on the path parts just to be safe
            if any(should_exclude_dir(part) for part in rel_dir.parts):
                continue
                
            src_file = Path(dirpath) / fname
            rel_file = rel_dir / fname
            files_to_copy.append((src_file, rel_file))
            
    print(f"Found {len(files_to_copy)} files to mirror.")
    
    # 2. Copy files to each destination
    for dest_dir in DEST_DIRS:
        print(f"\nMirroring to: {dest_dir}")
        if dest_dir.exists():
            print(f"Destination {dest_dir} already exists. Cleaning up existing files...")
            # We can remove existing files to ensure a clean mirror, but not the directory itself if we don't have to
            shutil.rmtree(dest_dir)
        
        dest_dir.mkdir(parents=True, exist_ok=True)
        
        copied_count = 0
        total_bytes = 0
        
        for src_file, rel_file in files_to_copy:
            target_file = dest_dir / rel_file
            target_file.parent.mkdir(parents=True, exist_ok=True)
            
            shutil.copy2(src_file, target_file)
            copied_count += 1
            total_bytes += src_file.stat().st_size
            
        print(f"Successfully mirrored {copied_count} files ({total_bytes} bytes) to {dest_dir}.")

if __name__ == "__main__":
    mirror()
