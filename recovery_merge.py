#!/usr/bin/env python3
"""
Btrfs recovery merge script.
Merges recovered files from recovery tree into main filesystem.
Moves everything, never overwrites, replaces broken symlinks.
"""
import os
import sys
import shutil
import argparse
import datetime

def log(msg, log_file=None):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{ts}] {msg}"
    print(formatted)
    if log_file:
        try:
            log_file.write(formatted + "\n")
            log_file.flush()
        except Exception:
            pass

def is_broken_symlink(path):
    return os.path.islink(path) and not os.path.exists(path)

def move_file_or_dir(src, dst):
    """
    Moves src to dst.
    Uses os.replace for atomic rename on same filesystem.
    Falls back to copy + delete for cross-device moves.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        # Cross-device fallback
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True)
            shutil.rmtree(src)
        else:
            shutil.copy2(src, dst)
            os.remove(src)

def merge_to_active(src, dst, log_file=None):
    """
    Recursively merge src directory tree into dst.
    Moves files/symlinks. Does not overwrite existing files (logs and skips them).
    Allows replacing a broken symlink at dst with the source file/dir.
    """
    if not os.path.exists(src) and not os.path.islink(src):
        return

    # If dst is a broken symlink, allow replacing it
    if is_broken_symlink(dst):
        log(f"[broken symlink] replacing broken symlink at {dst} with {src}", log_file)
        try:
            os.remove(dst)
        except Exception as e:
            log(f"Error removing broken symlink {dst}: {e}", log_file)

    if not os.path.lexists(dst):
        try:
            move_file_or_dir(src, dst)
            return
        except Exception as e:
            log(f"Error moving {src} -> {dst}: {e}", log_file)
            return

    # If dst exists and is a directory (and src is a directory), we merge contents recursively
    if os.path.isdir(src) and os.path.isdir(dst) and not os.path.islink(dst):
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(dst, item)
            merge_to_active(s, d, log_file)
        # Clean up empty directory in recovery tree
        try:
            if not os.listdir(src):
                os.rmdir(src)
        except Exception:
            pass
    else:
        # Collision! Destination exists (file or symlink) and cannot be merged
        log(f"[collision] skipping {src} -> {dst} (destination already exists)", log_file)

def main():
    parser = argparse.ArgumentParser(description="Merge recovered directory tree with main filesystem")
    parser.add_argument("--src", default="/home/v/recovery/home", help="Source recovery tree directory")
    parser.add_argument("--dst", default="/home/v", help="Destination main filesystem directory")
    parser.add_argument("--log", default="/home/v/recovery/recovery_merge.log", help="Path to log file")
    args = parser.parse_args()

    src_dir = os.path.abspath(args.src)
    dst_dir = os.path.abspath(args.dst)
    log_path = os.path.abspath(args.log)

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        log_fh = open(log_path, "a", buffering=1)
    except Exception as e:
        print(f"Error opening log file {log_path}: {e}")
        sys.exit(1)

    log("=" * 64, log_fh)
    log("Recovery Merge started.", log_fh)
    log(f"  Source      : {src_dir}", log_fh)
    log(f"  Destination : {dst_dir}", log_fh)
    log("=" * 64, log_fh)

    if not os.path.isdir(src_dir):
        log(f"Error: Source directory {src_dir} does not exist.", log_fh)
        sys.exit(1)

    try:
        merge_to_active(src_dir, dst_dir, log_fh)
        log("Recovery Merge completed successfully.", log_fh)
    except Exception as e:
        log(f"Fatal error during merge: {e}", log_fh)
        import traceback
        log(traceback.format_exc(), log_fh)
        sys.exit(1)
    finally:
        log_fh.close()

if __name__ == "__main__":
    main()
