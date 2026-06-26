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

def recursive_chown(path):
    """
    Recursively changes ownership of path to the invoking sudo user (SUDO_UID/SUDO_GID).
    If not running via sudo, does nothing.
    """
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if not sudo_uid or not sudo_gid:
        return
    try:
        uid = int(sudo_uid)
        gid = int(sudo_gid)
    except (ValueError, TypeError):
        return

    try:
        if os.path.lexists(path):
            os.chown(path, uid, gid, follow_symlinks=False)
        if os.path.isdir(path) and not os.path.islink(path):
            for root, dirs, files in os.walk(path, followlinks=False):
                try:
                    os.chown(root, uid, gid, follow_symlinks=False)
                except Exception:
                    pass
                for d in dirs:
                    try:
                        os.chown(os.path.join(root, d), uid, gid, follow_symlinks=False)
                    except Exception:
                        pass
                for f in files:
                    try:
                        os.chown(os.path.join(root, f), uid, gid, follow_symlinks=False)
                    except Exception:
                        pass
    except Exception:
        pass

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
    parent = os.path.dirname(dst)
    new_dirs = []
    curr = parent
    while curr and curr != "/" and not os.path.exists(curr):
        new_dirs.append(curr)
        curr = os.path.dirname(curr)

    os.makedirs(parent, exist_ok=True)
    
    for d in new_dirs:
        recursive_chown(d)

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

    recursive_chown(dst)

def redirect_home_system_dir(path, src=None):
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    parts = parent.split(os.sep)
    is_home = (parent == os.path.expanduser("~") or (len(parts) == 3 and parts[1] == 'home'))
    if is_home:
        name = os.path.basename(abs_path)
        mapping = {
            "sbin": os.path.join(".local", "sbin"),
            "bin": os.path.join(".local", "bin"),
            "lib": os.path.join(".local", "lib"),
            "libexec": os.path.join(".local", "libexec"),
            "include": os.path.join(".local", "include"),
            "share": os.path.join(".local", "share"),
            "ssl": os.path.join(".local", "share", "ssl"),
            "local": ".local"
        }
        if name in mapping:
            return os.path.join(parent, mapping[name])

        # Autohandle/redirect misplaced golang.org/x/tools components
        if src and os.path.isdir(src):
            go_tools_names = {"cmd", "internal", "refactor", "present", "playground"}
            if name in go_tools_names:
                is_x_tools = False
                if name == "cmd":
                    is_x_tools = any(os.path.exists(os.path.join(src, sub)) for sub in ["goyacc", "digraph"])
                elif name == "internal":
                    is_x_tools = any(os.path.exists(os.path.join(src, sub)) for sub in ["typesinternal", "facts", "gcimporter"])
                elif name == "refactor":
                    is_x_tools = any(os.path.exists(os.path.join(src, sub)) for sub in ["eg", "rename"])
                elif name == "present":
                    is_x_tools = os.path.exists(os.path.join(src, "testdata"))
                elif name == "playground":
                    is_x_tools = os.path.exists(os.path.join(src, "socket"))

                if is_x_tools:
                    return os.path.join(parent, "go", "src", "golang.org", "x", "tools", name)
    return path

def merge_to_active(src, dst, log_file=None, top_level=False):
    """
    Recursively merge src directory tree into dst.
    Moves files/symlinks. Does not overwrite existing files (logs and skips them).
    Allows replacing a broken symlink at dst with the source file/dir.
    """
    if not os.path.exists(src) and not os.path.islink(src):
        return

    if top_level:
        dst = redirect_home_system_dir(dst, src)

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
            if top_level:
                redirected_d = redirect_home_system_dir(d, s)
                if redirected_d != d:
                    log(f"[redirect] Redirecting system directory merge: {item} -> {redirected_d}", log_file)
                    d = redirected_d
            else:
                # Redirect misplaced golang.org/x/tools/go/... subdirectories (if merging inside ~/go)
                parent_abs = os.path.abspath(dst)
                go_dir = os.path.join(os.path.expanduser("~"), "go")
                if parent_abs == go_dir and item in {"analysis", "ast", "callgraph"}:
                    is_x_tools = False
                    if item == "analysis":
                        is_x_tools = any(os.path.exists(os.path.join(s, sub)) for sub in ["passes", "analysistest"])
                    elif item == "ast":
                        is_x_tools = any(os.path.exists(os.path.join(s, sub)) for sub in ["astutil", "inspector"])
                    elif item == "callgraph":
                        is_x_tools = any(os.path.exists(os.path.join(s, sub)) for sub in ["cha", "rta", "static"])
                    
                    if is_x_tools:
                        target_dst = os.path.join(os.path.expanduser("~"), "go", "src", "golang.org", "x", "tools", "go", item)
                        log(f"[redirect] Redirecting misplaced Go tools subdir: {item} -> {target_dst}", log_file)
                        d = target_dst
            merge_to_active(s, d, log_file, top_level=False)
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
    default_active = os.path.expanduser("~")
    default_src = os.path.join(default_active, "recovery", "recreated_dir_tree")
    default_log = os.path.join(default_active, "recovery", "recovery_merge.log")
    parser.add_argument("--src", default=default_src, help="Source recovery tree directory")
    parser.add_argument("--dst", default=default_active, help="Destination main filesystem directory")
    parser.add_argument("--log", default=default_log, help="Path to log file")
    args = parser.parse_args()

    src_dir = os.path.abspath(args.src)
    dst_dir = os.path.abspath(args.dst)
    log_path = os.path.abspath(args.log)

    parent = os.path.dirname(log_path)
    new_dirs = []
    curr = parent
    while curr and curr != "/" and not os.path.exists(curr):
        new_dirs.append(curr)
        curr = os.path.dirname(curr)

    os.makedirs(parent, exist_ok=True)
    for d in new_dirs:
        recursive_chown(d)

    try:
        log_fh = open(log_path, "a", buffering=1)
        recursive_chown(log_path)
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
        merge_to_active(src_dir, dst_dir, log_fh, top_level=True)
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
