#!/usr/bin/env python3
"""
Btrfs recovery cleanup script.
Optimised version: unified symlink restoration + squashing, single-pass dedup,
pruned walk excludes, hashed active-file lookup, continuous log file.
"""
import os
import re
import sys
import json
import tarfile
import hashlib
import shutil
import stat
import subprocess
import shlex
import datetime
from collections import defaultdict

# Try importing clean_file_merge natively, fallback to dev path if not installed
try:
    import clean_file_merge
    HAS_MERGE_MODULE = True
except ImportError:
    # Add the local development path to sys.path
    sys.path.insert(0, "/home/v/Projects/clean-file-merge/src")
    try:
        import clean_file_merge
        HAS_MERGE_MODULE = True
    except ImportError:
        HAS_MERGE_MODULE = False


# ── Paths ─────────────────────────────────────────────────────────────────────
ACTIVE_ROOT      = os.path.expanduser("~")
RECOVERY_DIR     = os.path.join(ACTIVE_ROOT, "recovery")
RECOVERED_ROOT   = os.path.join(RECOVERY_DIR, "recreated_dir_tree")
DUPLICATES_ROOT  = os.path.join(RECOVERY_DIR, "duplicates")
UNMATCHED_ROOT   = os.path.join(RECOVERY_DIR, "unmatched")
LOG_FILE         = os.path.join(RECOVERY_DIR, "clean_recovery.log")
STATS_FILE       = os.path.join(RECOVERY_DIR, "run_stats.json")
HISTORY_LOG      = os.path.join(RECOVERY_DIR, "history.log")   # never archived
ARCHIVES_DIR     = os.path.join(RECOVERY_DIR, "archives")

# clean-file-merge dedicated log directory (sibling project)
CFM_DIR          = os.path.join(ACTIVE_ROOT, "clean-file-merge")
CFM_LOGS_DIR     = os.path.join(CFM_DIR, "logs")

# Run output dirs (existence = previous run present)
RUN_DIRS = [RECOVERED_ROOT, DUPLICATES_ROOT, UNMATCHED_ROOT]

# Legacy paths – migrated on first run
LEGACY_RECOVERED_ROOT  = os.path.join(os.path.expanduser("~"), "recovered_home")
LEGACY_DUPLICATES_ROOT = os.path.join(os.path.expanduser("~"), "recovered_duplicates")
LEGACY_UNMATCHED_ROOT  = os.path.join(os.path.expanduser("~"), "recovered_unmatched")

HASH_BLOCK_SIZE = 65536

_log_file = None
args = None


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


def is_null_file(path):
    """Return True if the file exists, has size > 0, and contains only null bytes."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
            return False
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                if any(chunk):
                    return False
        return True
    except Exception:
        return False


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    print(msg)
    if _log_file:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            _log_file.write(f"[{ts}] {msg}\n")
            _log_file.flush()
        except Exception:
            pass

def history_log(msg):
    """Write timestamped entry to the history log file (always outside run archive)."""
    try:
        os.makedirs(os.path.dirname(HISTORY_LOG), exist_ok=True)
        # Note: we will chown HISTORY_LOG's parent directory and the file itself in main
        ts = datetime.datetime.now().isoformat()
        with open(HISTORY_LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# ── Persistent run stats ──────────────────────────────────────────────────────
# Cumulative integer counters
_STAT_KEYS = [
    "runs",
    "symlinks_restored", "dirs_squashed", "tombstones_removed",
    "symlink_restore_errors",
    "active_files_scanned",
    "lf_dirs_matched_hash", "lf_dirs_matched_structural",
    "dirs_merged",
    "files_checked_dedup", "duplicates_moved", "unique_files",
    "empty_dirs_cleaned",
    "unmatched_entries_moved",
    "cargo_dirs_moved",
    "corrupted_null_files_removed",
]

def load_stats():
    """Load full stats dict from disk (cumulative ints + boolean flags)."""
    base = {k: 0 for k in _STAT_KEYS}
    base["last_run_completed"] = False
    base["last_run_archived"]  = False
    try:
        with open(STATS_FILE) as f:
            saved = json.load(f)
        base.update(saved)   # preserves extra keys
    except Exception:
        pass
    return base

def save_stats(totals):
    """Persist full stats dict to disk."""
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(totals, f, indent=2)
        recursive_chown(STATS_FILE)
    except Exception as e:
        log(f"  [stats save error] {e}")

def _fmt(this_run, total, unit=""):
    """Format a stat line: 'N this run  (M total)' or just 'N' when both are equal."""
    u = f" {unit}" if unit else ""
    if total == this_run:
        return f"{this_run:,}{u}"
    return f"{this_run:,}{u}  (+{total:,} total)"


# ── Desktop notifications ─────────────────────────────────────────────────────
class DesktopNotifier:
    def __init__(self, title="Btrfs Recovery Cleanup"):
        self.title = title
        self.notify_id = None
        self.sudo_user = os.environ.get("SUDO_USER")
        self.sudo_uid = os.environ.get("SUDO_UID")
        self.sudo_gid = os.environ.get("SUDO_GID")

    def notify(self, message):
        cmd = ["notify-send"]
        if self.notify_id:
            cmd.extend(["-r", str(self.notify_id)])
        cmd.extend(["-p", self.title, message])

        # If running via sudo, we need to run notify-send as the original user
        if self.sudo_user and self.sudo_uid:
            try:
                uid = self.sudo_uid
                dbus_addr = None
                
                if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
                    dbus_addr = os.environ["DBUS_SESSION_BUS_ADDRESS"]
                else:
                    # Find DBUS session bus address in the user's running processes
                    proc = subprocess.run(
                        ["pgrep", "-u", uid, "-x", "dbus-daemon"],
                        capture_output=True, text=True
                    )
                    pids = proc.stdout.strip().split()
                    for pid in pids:
                        try:
                            with open(f"/proc/{pid}/environ", "rb") as env_file:
                                env_data = env_file.read()
                            for env_var in env_data.split(b"\x00"):
                                if env_var.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                                    dbus_addr = env_var.split(b"=", 1)[1].decode("utf-8", errors="ignore")
                                    break
                        except Exception:
                            continue
                        if dbus_addr:
                            break
                
                if not dbus_addr:
                    dbus_addr = f"unix:path=/run/user/{uid}/bus"

                env = os.environ.copy()
                env["DBUS_SESSION_BUS_ADDRESS"] = dbus_addr
                env["USER"] = self.sudo_user
                env["HOME"] = f"/home/{self.sudo_user}"

                full_cmd = ["sudo", "-u", self.sudo_user, "env", f"DBUS_SESSION_BUS_ADDRESS={dbus_addr}", "notify-send"]
                if self.notify_id:
                    full_cmd.extend(["-r", str(self.notify_id)])
                full_cmd.extend(["-p", self.title, message])

                res = subprocess.run(full_cmd, capture_output=True, text=True, env=env)
                if res.returncode == 0:
                    out = res.stdout.strip()
                    if out.isdigit():
                        self.notify_id = int(out)
                    return
            except Exception:
                pass

        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                out = res.stdout.strip()
                if out.isdigit():
                    self.notify_id = int(out)
        except Exception:
            pass


# ── User Prompt Helper ────────────────────────────────────────────────────────
def user_prompt(prompt_str, default, valid):
    """Prompt the user for input, defaulting to default on empty or EOF response."""
    valid = set(valid)
    while True:
        try:
            raw = input(prompt_str).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return default
        if not raw:
            return default
        if raw[0] in valid:
            return raw[0]
        print(f"  Please enter one of: {' / '.join(valid.upper())}")


def prompt_or_flag(prompt_str, default, valid, flag_name=None):
    """Get answer from command-line flag if set, otherwise prompt the user."""
    if flag_name and args and getattr(args, flag_name) is not None:
        val = getattr(args, flag_name).strip().lower()
        if val in valid:
            print(f"{prompt_str}[Pre-specified: {val}]")
            return val
    return user_prompt(prompt_str, default, valid)


# ── Run-state detection ───────────────────────────────────────────────────────
def run_dirs_exist():
    """True if at least one output directory is non-empty."""
    return any(os.path.isdir(d) and os.listdir(d) for d in RUN_DIRS)

def detect_run_state():
    """
    Returns:
      'none'       – no output dirs found
      'complete'   – dirs exist and last run finished successfully
      'incomplete' – dirs exist but run did not finish
    """
    if not run_dirs_exist():
        return 'none'

    # Check if the log file contains errors. If it does, consider the run incomplete/failed.
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'rb') as f:
                log_content = f.read()
            if b"embedded null character" in log_content or b"Error restoring symlink" in log_content:
                return 'incomplete'
        except Exception:
            pass

    # Primary: stats file flag
    try:
        stats = load_stats()
        return 'complete' if stats.get("last_run_completed") else 'incomplete'
    except Exception:
        pass
    # Fallback: tail of log file
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'rb') as f:
                f.seek(max(0, os.path.getsize(LOG_FILE) - 4000))
                tail = f.read().decode('utf-8', errors='ignore')
            return 'complete' if 'Done!  Run #' in tail else 'incomplete'
        except Exception:
            pass
    return 'incomplete'


# ── Archive / delete helpers ──────────────────────────────────────────────────
def _cfm_symlinks_in_recovery():
    """Return list of (symlink_path, real_target) for all cfm session symlinks in RECOVERY_DIR."""
    result = []
    try:
        for entry in os.scandir(RECOVERY_DIR):
            if entry.name.startswith("cfm_session_") and entry.is_symlink():
                target = os.readlink(entry.path)
                result.append((entry.path, target))
    except Exception:
        pass
    return result


def archive_run(label=""):
    """
    Pack output dirs + per-run log + stats into
    ARCHIVES_DIR/run_<label>_<timestamp>.tar.gz.
    For cfm session logs linked via symlinks in RECOVERY_DIR:
      - squash (copy real content) into the archive under cfm_logs/<name>
      - leave the real file at its location in ~/clean-file-merge/logs/
      - leave the symlink in RECOVERY_DIR intact
    Returns archive path on success, None on failure.
    """
    os.makedirs(ARCHIVES_DIR, exist_ok=True)
    recursive_chown(ARCHIVES_DIR)
    ts   = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    slug = f"_{label}" if label else ""
    dest = os.path.join(ARCHIVES_DIR, f"run{slug}_{ts}.tar.gz")
    print(f"  Archiving run to {dest} …")
    history_log(f"Archiving run → {dest}")
    try:
        with tarfile.open(dest, "w:gz") as tar:
            for path in [RECOVERED_ROOT, DUPLICATES_ROOT, UNMATCHED_ROOT,
                         LOG_FILE, STATS_FILE]:
                if os.path.exists(path):
                    arcname = os.path.relpath(path, RECOVERY_DIR)
                    tar.add(path, arcname=arcname)
            # Squash-include cfm session logs (resolve symlinks, add real content)
            for link_path, real_target in _cfm_symlinks_in_recovery():
                resolved = real_target if os.path.isabs(real_target) else \
                           os.path.normpath(os.path.join(RECOVERY_DIR, real_target))
                if os.path.isfile(resolved):
                    arcname = os.path.join("cfm_logs", os.path.basename(link_path))
                    tar.add(resolved, arcname=arcname)
                    history_log(f"  Squash-included cfm log: {resolved} -> {arcname}")
        mb = os.path.getsize(dest) / 1024 / 1024
        print(f"  Archive ready: {dest}  ({mb:.1f} MB)")
        recursive_chown(dest)
        history_log(f"Archive created: {dest} ({mb:.1f} MB)")
        return dest
    except Exception as e:
        print(f"  Archive FAILED: {e}")
        history_log(f"Archive FAILED: {e}")
        if os.path.exists(dest):
            try:
                os.remove(dest)
            except Exception:
                pass
        return None

def delete_run_dirs():
    """Remove output dirs, per-run log, and stats (keeps history.log and archives/)."""
    print("  Deleting run leftovers …")
    for path in [RECOVERED_ROOT, DUPLICATES_ROOT, UNMATCHED_ROOT, LOG_FILE, STATS_FILE]:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print(f"  Error deleting {path}: {e}")
    history_log("Run leftovers deleted.")
    print("  Done.")


# ── Startup check ─────────────────────────────────────────────────────────────
BAR = "─" * 64

def startup_check():
    """
    Inspect previous run state and let the user decide what to do.
    Returns 'run' (proceed) or calls sys.exit().
    """
    state = detect_run_state()
    stats = load_stats()

    if state == 'none':
        history_log("No previous run found. Starting fresh.")
        return 'run'

    if state == 'incomplete':
        print(f"\n{BAR}")
        print("⚠   Unfinished run detected:")
        for d in RUN_DIRS:
            if os.path.isdir(d) and os.listdir(d):
                print(f"    {d}")
        if os.path.exists(LOG_FILE):
            print(f"    Log : {LOG_FILE}")
        print()
        ans = prompt_or_flag(
            "Resume?  [Y]es / [n]o (delete) / [a]rchive : ",
            default="y", valid="yna", flag_name="resume"
        )
        print()
        if ans == "y":
            history_log("User resumed unfinished run.")
            print("  Resuming …")
        elif ans == "n":
            history_log("User deleted unfinished run leftovers.")
            delete_run_dirs()
        else:  # a
            arc = archive_run(label="unfinished")
            if arc:
                delete_run_dirs()
            else:
                print("  Archive failed — resuming on existing data.")
                history_log("Archive failed; resuming on existing data.")
        print(BAR + "\n")
        return 'run'

    # state == 'complete'
    print(f"\n{BAR}")
    print("✔   Previous completed run found.")
    archived = bool(stats.get("last_run_archived"))
    if not archived:
        # Check archives dir as fallback
        archived = (os.path.isdir(ARCHIVES_DIR) and
                    bool(os.listdir(ARCHIVES_DIR)))
    if archived:
        print("    (Results were archived previously.)")
        ans = prompt_or_flag("Delete leftovers? [Y/n]: ", default="y", valid="yn", flag_name="wipe")
        if ans == "y":
            delete_run_dirs()
    else:
        print("    (Results were NOT archived.)")
        ans = prompt_or_flag("Archive and wipe leftovers? [y/N]: ", default="n", valid="yn", flag_name="archive")
        if ans == "y":
            arc = archive_run()
            if arc:
                delete_run_dirs()
        else:
            ans2 = prompt_or_flag("Delete leftovers without archiving? [Y/n]: ",
                               default="y", valid="yn", flag_name="wipe")
            if ans2 == "y":
                delete_run_dirs()
            else:
                print("  Leftovers kept — new run will merge into existing data.")
                history_log("User kept completed run leftovers; will merge.")
    print(BAR + "\n")
    return 'run'


# ── Shutdown prompt ───────────────────────────────────────────────────────────
def shutdown_prompt(success, notifier, symlink_errors, totals):
    """
    Always called before the process exits (success or failure).
    Asks user whether to archive results and/or wipe leftovers.
    If success and no symlink errors, also asks to run recovery merge.
    """
    print(BAR)
    if success:
        print("✔   Run completed successfully.")
    else:
        print("❌  Run failed.")
    print(BAR)

    # ── Recovery Merge Prompt ──────────────────────────────────────────────────
    has_errors = not success or symlink_errors > 0 or totals.get('symlink_restore_errors', 0) > 0
    if not has_errors:
        ans_merge = prompt_or_flag("Run recovery merge into active filesystem? [y/N]: ", default="n", valid="yn", flag_name="merge")
        if ans_merge == "y":
            log("Running clean-file-merge...")
            notifier.notify("Running recovery merge...")
            
            if HAS_MERGE_MODULE:
                try:
                    # cfm writes its own session log in ~/clean-file-merge/logs/
                    cfm_log_path = clean_file_merge.get_session_log_path()
                    cfm_log_fh   = open(cfm_log_path, "a", buffering=1)
                    clean_file_merge.merge_to_active(
                        RECOVERED_ROOT, ACTIVE_ROOT,
                        log_file=cfm_log_fh, top_level=True
                    )
                    cfm_log_fh.close()
                    clean_file_merge.history_log(f"Session completed OK -> {cfm_log_path}")
                    # Symlink cfm session log into ~/recovery/ for cross-reference
                    link_name = os.path.join(
                        RECOVERY_DIR,
                        f"cfm_{os.path.basename(cfm_log_path)}"
                    )
                    if not os.path.lexists(link_name):
                        try:
                            os.symlink(cfm_log_path, link_name)
                            log(f"    Linked cfm log: {link_name} -> {cfm_log_path}")
                        except Exception as sym_err:
                            log(f"    [warn] could not create cfm log symlink: {sym_err}")
                    log("✔   Clean File Merge completed successfully.")
                    notifier.notify("Recovery merge completed.")
                except Exception as e:
                    log(f"❌  Error executing native merge: {e}")
                    notifier.notify("Recovery merge execution error.")
            else:
                # Fallback to subprocess CLI execution if module not found
                merge_script = shutil.which("clean-file-merge")
                if not merge_script:
                    dev_path = "/home/v/Projects/clean-file-merge/src/clean_file_merge/main.py"
                    if os.path.exists(dev_path):
                        merge_script = dev_path

                if not merge_script:
                    log("❌  Error: clean-file-merge dependency not found! Please install it or make sure it is in your PATH.")
                    notifier.notify("Merge failed: dependency clean-file-merge not found.")
                else:
                    # Determine cfm session log path (next available in ~/clean-file-merge/logs/)
                    os.makedirs(CFM_LOGS_DIR, exist_ok=True)
                    n = 1
                    while True:
                        cfm_log_path = os.path.join(CFM_LOGS_DIR, f"session_{n:03d}.log")
                        if not os.path.exists(cfm_log_path):
                            break
                        n += 1
                    if merge_script.endswith(".py"):
                        cmd = ["sudo", "python3", merge_script, "--src", RECOVERED_ROOT, "--dst", ACTIVE_ROOT, "--log", cfm_log_path]
                    else:
                        cmd = ["sudo", merge_script, "--src", RECOVERED_ROOT, "--dst", ACTIVE_ROOT, "--log", cfm_log_path]
                    try:
                        res = subprocess.run(cmd, capture_output=True, text=True)
                        for line in res.stdout.splitlines():
                            log(f"  [merge] {line}")
                        if res.returncode == 0:
                            log("✔   Clean File Merge completed successfully.")
                            notifier.notify("Recovery merge completed.")
                            # Symlink cfm session log into ~/recovery/ for cross-reference
                            link_name = os.path.join(
                                RECOVERY_DIR,
                                f"cfm_{os.path.basename(cfm_log_path)}"
                            )
                            if not os.path.lexists(link_name) and os.path.isfile(cfm_log_path):
                                try:
                                    os.symlink(cfm_log_path, link_name)
                                    log(f"    Linked cfm log: {link_name} -> {cfm_log_path}")
                                except Exception as sym_err:
                                    log(f"    [warn] could not create cfm log symlink: {sym_err}")
                        else:
                            log(f"❌  Clean File Merge failed (exit code {res.returncode}):")
                            for line in res.stderr.splitlines():
                                log(f"  [merge err] {line}")
                            notifier.notify("Recovery merge failed.")
                    except Exception as e:
                        log(f"❌  Error executing merge: {e}")
                        notifier.notify("Recovery merge execution error.")
    else:
        if success:
            log("⚠   Skipping recovery merge due to symlink restoration warnings/errors.")
        else:
            log("❌  Skipping recovery merge due to run failure.")

    ans = prompt_or_flag("Archive results before exit? [y/N]: ", default="n", valid="yn", flag_name="archive")
    if ans == "y":
        arc = archive_run(label="success" if success else "failed")
        if arc:
            # Mark archived in stats
            try:
                st = load_stats()
                st["last_run_archived"] = True
                save_stats(st)
            except Exception:
                pass
            history_log(f"Results archived after {'success' if success else 'failure'}.")
            # Wipe prompt (default differs: N after success, Y after failure)
            if success:
                w = prompt_or_flag("Wipe leftovers? [y/N]: ", default="n", valid="yn", flag_name="wipe")
            else:
                w = prompt_or_flag("Wipe partial leftovers? [Y/n]: ", default="y", valid="yn", flag_name="wipe")
            if w == "y":
                delete_run_dirs()
                notifier.notify("Archived & wiped.")
            else:
                notifier.notify("Archived. Leftovers kept.")
        else:
            print("  Archive failed — leftovers kept.")
    else:
        history_log("User chose not to archive results.")
        print("  Leftovers left as-is.")
    print(BAR)


def move_file_or_dir(src, dst):
    """
    Guaranteed to move src to dst.
    If it's on the same filesystem, uses os.replace (atomic rename).
    If it's cross-device, copies and then explicitly deletes the source.
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


def run_recheck_duplicates(active_files_by_size, active_hash_cache, notifier):
    log("\nRechecking for leftover duplicate files in recovery and unmatched directories...")
    notifier.notify("Rechecking leftover duplicates...")
    
    dup_moved = 0
    cleaned_dirs = 0

    # Scan RECOVERED_ROOT and UNMATCHED_ROOT
    roots_to_scan = [RECOVERED_ROOT, UNMATCHED_ROOT]
    files_to_check = []
    
    for r in roots_to_scan:
        if os.path.isdir(r):
            for root, dirs, files in os.walk(r, followlinks=False):
                # Avoid scanning DUPLICATES_ROOT if it's somehow inside (it shouldn't be)
                abs_root = os.path.realpath(root)
                if abs_root == DUPLICATES_ROOT or abs_root.startswith(DUPLICATES_ROOT + os.sep):
                    dirs[:] = []
                    continue
                for fname in files:
                    path = os.path.join(root, fname)
                    try:
                        st = os.lstat(path)
                        if stat.S_ISREG(st.st_mode):
                            files_to_check.append(path)
                    except Exception:
                        pass

    log(f"  Found {len(files_to_check):,} files in recovery to check for duplicates.")
    
    for idx, path in enumerate(files_to_check):
        if (idx + 1) % 10000 == 0 or idx == 0 or (idx + 1) == len(files_to_check):
            pct = ((idx + 1) / len(files_to_check)) * 100
            log(f"  Checking file {idx+1}/{len(files_to_check)} ({pct:.1f}%)...")
            
        try:
            st = os.lstat(path)
            size = st.st_size
        except Exception:
            continue
            
        if size not in active_files_by_size:
            continue
            
        rec_hash = get_file_hash(path)
        if not rec_hash:
            continue
            
        # Find if it duplicates any active file
        duplicate_active_path = None
        for active_path in active_files_by_size[size]:
            if active_path not in active_hash_cache:
                h = get_file_hash(active_path)
                if h:
                    active_hash_cache[active_path] = h
            if active_hash_cache.get(active_path) == rec_hash:
                duplicate_active_path = active_path
                break
                
        if duplicate_active_path:
            # It's a duplicate! Move to DUPLICATES_ROOT
            rel = os.path.relpath(duplicate_active_path, ACTIVE_ROOT)
            dst = os.path.join(DUPLICATES_ROOT, rel)
            try:
                move_file_or_dir(path, dst)
                dup_moved += 1
                log(f"  [cleanup duplicate] moved duplicate {path} -> {dst}")
            except Exception as e:
                log(f"  [cleanup duplicate error] failed to move {path}: {e}")

    # Clean up empty directories in RECOVERED_ROOT and UNMATCHED_ROOT
    for r in roots_to_scan:
        if os.path.isdir(r):
            for root, dirs, files in os.walk(r, topdown=False, followlinks=False):
                if root == r:
                    continue
                try:
                    if not os.listdir(root):
                        os.rmdir(root)
                        cleaned_dirs += 1
                except Exception:
                    pass

    log(f"Recheck duplicate cleanup done: moved {dup_moved} duplicate files and cleaned {cleaned_dirs} empty directories.")
    return dup_moved, cleaned_dirs


# ── Hashing ───────────────────────────────────────────────────────────────────
def get_file_hash(path):
    """Calculate SHA-256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(HASH_BLOCK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


# ── Symlink detection ─────────────────────────────────────────────────────────
# Valid Unix path chars only – rejects binary garbage
_VALID_PATH_RE = re.compile(r'^[\w./\-@:~+]+$')

def looks_like_symlink_target(raw_bytes):
    """
    Given raw bytes from a small file, return the symlink target string if the
    file looks like a wrongly-recovered symlink, else None.
    """
    if b'\x00' in raw_bytes:
        return None
    try:
        text = raw_bytes.decode('utf-8').strip()
    except UnicodeDecodeError:
        return None
    if not text or '\n' in text or ' ' in text:
        return None
    if len(text) >= 256 or len(text) < 2:
        return None
    if text.startswith('{'):
        return None
    if not _VALID_PATH_RE.match(text):
        return None
    if (text.startswith("data/") or text.startswith("/") or
            text == "steam" or text.startswith("../") or
            (text.startswith(".") and "/" in text)):
        return text
    return None

def try_read_as_symlink(path, st=None):
    """
    If *path* is a tiny regular file whose content looks like a symlink target,
    return (content_str, stat_result).  Otherwise return (None, st).
    """
    # Exclude files that are definitely not symlinks (e.g. build files or standard code/data extensions)
    name = os.path.basename(path).lower()
    if name.endswith(('.make', '.marks', '.cmake', '.ninja', '.o', '.a', '.d', '.py', '.cpp', '.h', '.c', '.json')):
        return None, st
    if 'cmakefiles' in path.lower() or '.git/' in path:
        return None, st

    try:
        if st is None:
            st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return None, st
        size = st.st_size
        if size == 0 or size > 512:
            return None, st
        with open(path, 'rb') as fh:
            raw = fh.read(512)
        target = looks_like_symlink_target(raw)
        return target, st
    except Exception:
        return None, st


# ── Step 0: unified restore + squash ─────────────────────────────────────────
def step0_restore_and_squash(root_dir, notifier):
    """
    Single os.walk pass that:
      1. Finds text-file fake symlinks in *filenames* → restores them as real
         symlinks; if the target is a non-empty dir inside root_dir → squashes
         to a real directory.
      2. Finds real symlinks in *dirnames* → if target is a non-empty dir inside
         root_dir → squashes to a real directory.
      3. Collects a name→paths index while walking to efficiently find and delete
         tombstone files of squashed dirs (0-byte or text-file) without a second
         full walk.
    """
    log("Step 0: Restoring text-file symlinks and squashing dir-symlinks...")
    notifier.notify("Step 0: Restoring & squashing symlinks...")

    restored       = 0  # text-file → symlink (non-dir targets)
    squashed       = 0  # dir-symlinks squashed to real dirs
    tombstones     = 0
    symlink_errors = 0

    # Pass 1 – collect a name→[(dirpath, is_file)] index while also processing
    # real dir-symlinks and text-file symlinks that target directories.
    squashed_names  = set()   # names of squashed dirs (for tombstone search)
    name_to_paths   = defaultdict(list)  # filename → list of abs paths

    for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=False):

        # ── 2. Real symlinks in dirnames ──────────────────────────────────────
        for name in list(dirnames):
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                # Index regular dirs for tombstone lookup
                name_to_paths[name].append(path)
                continue
            target = os.readlink(path)
            target_abs = os.path.normpath(os.path.join(dirpath, target))
            if not target_abs.startswith(root_dir) or not os.path.isdir(target_abs):
                continue
            try:
                contents = os.listdir(target_abs)
            except Exception:
                continue
            if not contents:
                continue
            log(f"  [squash] {path} -> {target}  ({len(contents)} entries)")
            try:
                os.remove(path)
                if target_abs != path:
                    shutil.move(target_abs, path)
                squashed += 1
                squashed_names.add(name)
                name_to_paths[name].append(path)  # now a real dir
            except Exception as e:
                log(f"  [squash error] {path}: {e}")
                symlink_errors += 1

        # ── 1. Files: text-file symlinks ──────────────────────────────────────
        for name in list(filenames):
            path = os.path.join(dirpath, name)
            # Index all files for tombstone lookup
            name_to_paths[name].append(path)

            if os.path.islink(path):
                continue  # already a real symlink

            st = None
            try:
                st = os.lstat(path)
            except Exception:
                continue

            target, st = try_read_as_symlink(path, st)
            if not target:
                continue

            target_abs = os.path.normpath(os.path.join(dirpath, target))
            target_is_dir = os.path.isdir(target_abs)

            # Restore as real symlink first
            try:
                os.remove(path)
            except Exception as e:
                log(f"  [restore error] cannot remove {path}: {e}")
                symlink_errors += 1
                continue

            if target_is_dir and target_abs.startswith(root_dir):
                try:
                    contents = os.listdir(target_abs)
                except Exception:
                    contents = []
                if contents:
                    # Squash: make it a real directory
                    log(f"  [restore+squash] {path} -> {target}  ({len(contents)} entries)")
                    try:
                        shutil.move(target_abs, path)
                        squashed += 1
                        squashed_names.add(name)
                        restored += 1
                        continue
                    except Exception as e:
                        log(f"  [squash error after restore] {path}: {e}")
                        symlink_errors += 1
                        # Fall through: re-create symlink

            # (Re-)create symlink
            try:
                os.symlink(target, path)
                log(f"  [restore] {path} -> {target}")
                restored += 1
            except Exception as e:
                log(f"  [restore error] {path} -> {target}: {e}")
                symlink_errors += 1

    # Pass 2 – tombstone removal: only inspect files whose name matches a squashed dir
    for sname in squashed_names:
        for tpath in name_to_paths.get(sname, []):
            if os.path.islink(tpath) or os.path.isdir(tpath):
                continue
            if not os.path.isfile(tpath):
                continue
            try:
                sz = os.path.getsize(tpath)
                if sz == 0:
                    log(f"  [tombstone] removing 0-byte: {tpath}")
                    os.remove(tpath)
                    tombstones += 1
                elif sz <= 512:
                    with open(tpath, 'rb') as fh:
                        raw = fh.read(512)
                    t = looks_like_symlink_target(raw)
                    if t:
                        log(f"  [tombstone] removing text stub: {tpath} (-> {t!r})")
                        os.remove(tpath)
                        tombstones += 1
            except Exception:
                pass

    log(f"Step 0 done: restored {restored} text-file symlinks, "
        f"squashed {squashed} dir-symlinks, removed {tombstones} tombstones, with {symlink_errors} errors.")
    return restored, squashed, tombstones, symlink_errors


# ── is_home_dir & redirect_home_system_dir ────────────────────────────────────
def is_home_dir(path):
    abs_path = os.path.abspath(path)
    if abs_path == os.path.expanduser("~"):
        return True
    parts = abs_path.split(os.sep)
    if len(parts) == 3 and parts[1] == 'home':
        return True
    return False

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

# ── merge_dirs ────────────────────────────────────────────────────────────────
def merge_dirs(src, dst, top_level=False):
    """Recursively merge src into dst (in-place rename when possible)."""
    if top_level:
        dst = redirect_home_system_dir(dst, src)
    # Resolve text-file symlink at dst if present
    if not os.path.islink(dst) and os.path.isfile(dst):
        t, _ = try_read_as_symlink(dst)
        if t:
            try:
                os.remove(dst)
                os.symlink(t, dst)
            except Exception:
                pass

    # Conflict: dst is a plain file but src is a directory
    if os.path.lexists(dst) and not os.path.isdir(dst) and not os.path.islink(dst):
        dst_bak = dst + ".file"
        log(f"  [conflict] {dst} is a file, renaming to {dst_bak}")
        try:
            if os.path.lexists(dst_bak):
                os.remove(dst_bak)
            os.rename(dst, dst_bak)
        except Exception as e:
            log(f"  [conflict error] {dst}: {e}")

    if not os.path.lexists(dst):
        try:
            move_file_or_dir(src, dst)
            return
        except Exception as e:
            log(f"  [merge rename error] {src} -> {dst}: {e}")

    if not os.path.lexists(dst):
        os.makedirs(dst, exist_ok=True)

    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if top_level:
            redirected_d = redirect_home_system_dir(d, s)
            if redirected_d != d:
                log(f"  [redirect] Redirecting system directory merge: {item} -> {redirected_d}")
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
                    log(f"  [redirect] Redirecting misplaced Go tools subdir: {item} -> {target_dst}")
                    d = target_dst
        if os.path.islink(s):
            # Preserve symlinks
            if not os.path.lexists(d):
                try:
                    os.symlink(os.readlink(s), d)
                    os.remove(s)
                except Exception:
                    pass
            else:
                # Target exists, s is a duplicate symlink, remove from src
                try:
                    os.remove(s)
                except Exception:
                    pass
        elif os.path.isdir(s):
            merge_dirs(s, d, top_level=False)
        else:
            if not os.path.lexists(d):
                try:
                    move_file_or_dir(s, d)
                except Exception as e:
                    log(f"  [move error] {s}: {e}")
            else:
                # Keep the larger file (likely the more complete recovery)
                try:
                    if not os.path.islink(d) and os.path.getsize(s) > os.path.getsize(d):
                        move_file_or_dir(s, d)
                    else:
                        # Move duplicate s to duplicates directory to keep recovery tree clean
                        rel = os.path.relpath(d, ACTIVE_ROOT)
                        dup_dst = os.path.join(DUPLICATES_ROOT, rel)
                        move_file_or_dir(s, dup_dst)
                except Exception as e:
                    log(f"  [merge duplicate error] {s}: {e}")
    try:
        os.rmdir(src)
    except Exception:
        pass


# ── Legacy migration ──────────────────────────────────────────────────────────
def migrate_legacy_dirs():
    os.makedirs(RECOVERY_DIR, exist_ok=True)
    recursive_chown(RECOVERY_DIR)
    
    # Also migrate old "home" subfolder to the new configured RECOVERED_ROOT
    old_recovered_root = os.path.join(RECOVERY_DIR, "home")
    if old_recovered_root != RECOVERED_ROOT and os.path.exists(old_recovered_root) and not os.path.exists(RECOVERED_ROOT):
        log(f"Migrating recovery subfolder name: {old_recovered_root} -> {RECOVERED_ROOT}")
        try:
            shutil.move(old_recovered_root, RECOVERED_ROOT)
        except Exception as e:
            log(f"Error migrating {old_recovered_root} -> {RECOVERED_ROOT}: {e}")

    for src, dst in [
        (LEGACY_RECOVERED_ROOT,  RECOVERED_ROOT),
        (LEGACY_DUPLICATES_ROOT, DUPLICATES_ROOT),
        (LEGACY_UNMATCHED_ROOT,  UNMATCHED_ROOT),
    ]:
        if os.path.exists(src) and not os.path.exists(dst):
            log(f"Migrating {src} -> {dst}")
            shutil.move(src, dst)
        elif os.path.exists(src) and os.path.exists(dst):
            log(f"Merging legacy {src} -> {dst}")
            merge_dirs(src, dst)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _log_file, args
    global ACTIVE_ROOT, RECOVERY_DIR, RECOVERED_ROOT, DUPLICATES_ROOT, UNMATCHED_ROOT
    global LOG_FILE, STATS_FILE, HISTORY_LOG, ARCHIVES_DIR, RUN_DIRS
    global LEGACY_RECOVERED_ROOT, LEGACY_DUPLICATES_ROOT, LEGACY_UNMATCHED_ROOT

    import argparse
    parser = argparse.ArgumentParser(description="Btrfs recovery cleanup script")
    parser.add_argument("--active-root", default=os.path.expanduser("~"), help="Path to main active filesystem directory")
    parser.add_argument("--recovery-dir", default=os.path.join(os.path.expanduser("~"), "recovery"), help="Path to base recovery directory")
    parser.add_argument("--recreated-dir-name", default="recreated_dir_tree", help="Name of recreated directory tree folder under recovery-dir")
    parser.add_argument("--duplicates-dir-name", default="duplicates", help="Name of duplicates folder under recovery-dir")
    parser.add_argument("--unmatched-dir-name", default="unmatched", help="Name of unmatched folder under recovery-dir")
    parser.add_argument("--resume", choices=["y", "n", "a"], help="Answer for resume prompt if unfinished run is detected")
    parser.add_argument("--archive", choices=["y", "n"], help="Answer for archiving prompt (at startup or shutdown)")
    parser.add_argument("--wipe", choices=["y", "n"], help="Answer for wiping leftovers prompt")
    parser.add_argument("--recheck-unmatched", action="store_true", help="Only recheck unmatched files in the unmatched/ directory from the last run")
    parser.add_argument("--recheck-duplicates", action="store_true", help="Scan recovery directories for files that duplicate active files and move them to duplicates/")
    parser.add_argument("--merge", choices=["y", "n"], help="Answer for automatically running recovery merge prompt")
    args = parser.parse_args()

    # Configure paths dynamically from CLI arguments
    ACTIVE_ROOT = os.path.abspath(args.active_root)
    RECOVERY_DIR = os.path.abspath(args.recovery_dir)
    RECOVERED_ROOT = os.path.join(RECOVERY_DIR, args.recreated_dir_name)
    DUPLICATES_ROOT = os.path.join(RECOVERY_DIR, args.duplicates_dir_name)
    UNMATCHED_ROOT = os.path.join(RECOVERY_DIR, args.unmatched_dir_name)

    LOG_FILE = os.path.join(RECOVERY_DIR, "clean_recovery.log")
    STATS_FILE = os.path.join(RECOVERY_DIR, "run_stats.json")
    HISTORY_LOG = os.path.join(RECOVERY_DIR, "history.log")
    ARCHIVES_DIR = os.path.join(RECOVERY_DIR, "archives")

    RUN_DIRS = [RECOVERED_ROOT, DUPLICATES_ROOT, UNMATCHED_ROOT]

    # Legacy paths relative to active root
    LEGACY_RECOVERED_ROOT  = os.path.join(os.path.dirname(ACTIVE_ROOT), "recovered_home")
    LEGACY_DUPLICATES_ROOT = os.path.join(os.path.dirname(ACTIVE_ROOT), "recovered_duplicates")
    LEGACY_UNMATCHED_ROOT  = os.path.join(os.path.dirname(ACTIVE_ROOT), "recovered_unmatched")

    os.makedirs(RECOVERY_DIR, exist_ok=True)
    recursive_chown(RECOVERY_DIR)
    notifier = DesktopNotifier()

    # ── Pre-run startup check (before opening the per-run log) ───────────────
    history_log("=" * 50)
    history_log("Program started.")
    startup_check()   # may delete/archive old data or just proceed

    # ── Open per-run log (append so resume continues where it left off) ───────
    _log_file = open(LOG_FILE, "a", buffering=1)
    recursive_chown(LOG_FILE)
    ts = datetime.datetime.now().isoformat()
    log(f"\n{'='*60}")
    log(f"Recovery cleanup started at {ts}")
    log(f"{'='*60}")

    # Mark run as in-progress so a crash is detectable next time
    _cur = load_stats()
    _cur["last_run_completed"] = False
    _cur["last_run_archived"]  = False
    save_stats(_cur)
    history_log(f"Run started at {ts}.")

    success = False
    # All these locals must exist even if an exception fires before they're set
    restored = squashed = tombstones_removed = symlink_errors = scanned_active = 0
    matched_dirs = struct_count = total_rec = dup_count = unique_count = 0
    cleanup_count = unmatched_count = cargo_moved = null_removed = 0
    renames_to_execute = {}
    totals = load_stats()   # will be overwritten on success

    try:
        # ── Migration ────────────────────────────────────────────────────────
        if not getattr(args, "recheck_unmatched", False) and not getattr(args, "recheck_duplicates", False):
            log("Checking for legacy output directories to migrate...")
            migrate_legacy_dirs()

            # ── Step 0: Restore text-file symlinks + squash dir-symlinks ─────────
            restored, squashed, tombstones_removed, symlink_errors = step0_restore_and_squash(
                RECOVERED_ROOT, notifier)

        # ── Step 1: Scan active filesystem ───────────────────────────────────
        notifier.notify("Step 1: Scanning active filesystem...")
        log("\nStep 1: Scanning active filesystem (excluding recovery paths)...")

        total_files_cache = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "total_files.txt")
        total_files_est = None
        if os.path.exists(total_files_cache):
            try:
                with open(total_files_cache) as f:
                    total_files_est = int(f.read().strip())
            except Exception:
                pass

        _excl_abs  = os.path.abspath(RECOVERY_DIR)
        _excl_sep  = _excl_abs + os.sep

        active_files_by_size = defaultdict(list)
        active_hash_cache    = {}

        scanned_active = 0
        for root, dirs, files in os.walk(ACTIVE_ROOT, followlinks=True):
            abs_root = os.path.realpath(root)
            if abs_root == _excl_abs or abs_root.startswith(_excl_sep):
                dirs[:] = []
                continue

            pruned_dirs = []
            for d in dirs:
                d_path = os.path.join(root, d)
                try:
                    d_real = os.path.realpath(d_path)
                except Exception:
                    continue
                if d_real == _excl_abs or d_real.startswith(_excl_sep):
                    continue
                # Exclude symlinks pointing outside ACTIVE_ROOT to prevent scanning external dirs
                if os.path.islink(d_path):
                    if not d_real.startswith(ACTIVE_ROOT + os.sep) and d_real != ACTIVE_ROOT:
                        continue
                pruned_dirs.append(d)
            dirs[:] = pruned_dirs

            for fname in files:
                path = os.path.join(root, fname)
                try:
                    st = os.lstat(path)
                except Exception:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                scanned_active += 1
                active_files_by_size[st.st_size].append(path)
                if total_files_est and scanned_active % 10000 == 0:
                    pct = min(99.9, (scanned_active / total_files_est) * 100)
                    log(f"  Scanned {scanned_active:,} active files ({pct:.1f}%)...")
        log(f"  Scanned {scanned_active:,} active files total.")

        # Update cache file with final scan count for future estimates
        try:
            with open(total_files_cache, "w") as f:
                f.write(str(scanned_active))
        except Exception:
            pass

        # ── Step 2: Match lost+found directories ─────────────────────────────
        if not getattr(args, "recheck_duplicates", False):
            log("\nStep 2: Matching lost+found directories by content...")
        notifier.notify("Step 2: Matching lost+found...")
        matched_dirs = 0
        struct_count = 0
        total_lfs = 0

        # Scan for standard user subdirectories to infer home folder
        known_home_subdirs = {
            ".cache", ".config", ".local", "Documents", "Downloads",
            "Pictures", "Music", "Videos", "Projects", "Desktop",
            "Templates", "Public"
        }

        lfs_source_dir = UNMATCHED_ROOT if getattr(args, "recheck_unmatched", False) else RECOVERED_ROOT
        if os.path.isdir(lfs_source_dir):
            lfs = sorted([d for d in os.listdir(lfs_source_dir)
                          if d.startswith("lost+found_")])
            total_lfs = len(lfs)
            log(f"  Found {total_lfs} lost+found directories in {lfs_source_dir} to process.")

            for idx, lf in enumerate(lfs):
                lf_path = os.path.join(lfs_source_dir, lf)
                if not os.path.isdir(lf_path):
                    continue

                if (idx + 1) % 500 == 0 or idx == 0 or (idx + 1) == len(lfs):
                    pct = ((idx + 1) / len(lfs)) * 100
                    log(f"  Processing {idx+1}/{len(lfs)} lost+found ({pct:.1f}%)...")

                # Subdirectory inference check
                try:
                    children = os.listdir(lf_path)
                except Exception:
                    continue

                # If child dir is standard user subdir (e.g. .cache) -> target home
                inferred_home = False
                for child in children:
                    if child in known_home_subdirs and os.path.isdir(os.path.join(lf_path, child)):
                        log(f"  [structural match] {lf} -> {RECOVERED_ROOT}  (via child {child})")
                        renames_to_execute[lf_path] = RECOVERED_ROOT
                        matched_dirs += 1
                        struct_count += 1
                        inferred_home = True
                        break
                if inferred_home:
                    continue

                # Fallback: File hash content matching
                candidates = defaultdict(int)
                for root, dirs, files in os.walk(lf_path, followlinks=False):
                    for fname in files:
                        lf_filepath = os.path.join(root, fname)
                        try:
                            st = os.lstat(lf_filepath)
                        except Exception:
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        size = st.st_size
                        if size not in active_files_by_size:
                            continue

                        # Check hash
                        lf_hash = get_file_hash(lf_filepath)
                        if not lf_hash:
                            continue

                        # Find matching active files
                        for active_path in active_files_by_size[size]:
                            if active_path not in active_hash_cache:
                                h = get_file_hash(active_path)
                                if h:
                                    active_hash_cache[active_path] = h
                            if active_hash_cache.get(active_path) == lf_hash:
                                rel = os.path.relpath(lf_filepath, lf_path)
                                if active_path.endswith(rel):
                                    inferred_dst = active_path[:-len(rel)].rstrip("/")
                                    candidates[inferred_dst] += 1

                if candidates:
                    best_dst = max(candidates, key=candidates.get)
                    votes = candidates[best_dst]
                    if votes >= 1:
                        redirected_dst = redirect_home_system_dir(best_dst)
                        if redirected_dst != best_dst:
                            log(f"  [redirect] System directory match redirected: {best_dst} -> {redirected_dst}")
                        log(f"  [match] {lf} -> {redirected_dst}  ({votes} files matched)")
                        renames_to_execute[lf_path] = redirected_dst
                        matched_dirs += 1

            if getattr(args, "recheck_unmatched", False):
                still_unmatched = total_lfs - matched_dirs
                pct_matched = (matched_dirs / total_lfs * 100) if total_lfs else 0.0
                log(f"\n  Rescanned unmatched summary:")
                log(f"    New matches                 : {matched_dirs} ({pct_matched:.1f}%)")
                log(f"    Still unmatched             : {still_unmatched} ({(100.0 - pct_matched):.1f}%)")
                log(f"    Ratio (matched/unmatched)   : {matched_dirs}/{still_unmatched}")

        # ── Step 3: Merge matched directories ────────────────────────────────
        if not getattr(args, "recheck_duplicates", False):
            log("\nStep 3: Merging matched directories into active locations...")
            notifier.notify("Step 3: Merging directories...")
            for src, dst in renames_to_execute.items():
                log(f"  [merge] {os.path.basename(src)} -> {dst}")
                merge_dirs(src, dst, top_level=True)

        # ── Step 4: Deduplicate recovered files against active disk ──────────
        if not getattr(args, "recheck_unmatched", False) and not getattr(args, "recheck_duplicates", False):
            log("\nStep 4: Deduplicating recovered files against active disk...")
            notifier.notify("Step 4: Deduplicating files...")
            
            # Single-pass collection of recovered files
            rec_files = []
            if os.path.isdir(RECOVERED_ROOT):
                for root, dirs, files in os.walk(RECOVERED_ROOT, followlinks=False):
                    abs_root = os.path.realpath(root)
                    if abs_root == DUPLICATES_ROOT or abs_root.startswith(DUPLICATES_ROOT + os.sep):
                        dirs[:] = []
                        continue
                    for fname in files:
                        path = os.path.join(root, fname)
                        try:
                            if is_null_file(path):
                                log(f"  [corrupted null file] removing: {path}")
                                os.remove(path)
                                null_removed += 1
                                continue
                            st = os.lstat(path)
                            if stat.S_ISREG(st.st_mode):
                                rec_files.append((path, st.st_size))
                        except Exception:
                            pass

            total_rec = len(rec_files)
            dup_count = 0
            unique_count = 0
            log(f"  Found {total_rec:,} recovered files to check for deduplication.")

            for idx, (path, size) in enumerate(rec_files):
                if (idx + 1) % 10000 == 0 or idx == 0 or (idx + 1) == total_rec:
                    pct = ((idx + 1) / total_rec) * 100
                    log(f"  Deduplicating {idx+1}/{total_rec} ({pct:.1f}%)...")

                if size not in active_files_by_size:
                    unique_count += 1
                    continue

                rec_hash = get_file_hash(path)
                if not rec_hash:
                    unique_count += 1
                    continue

                is_duplicate = False
                for active_path in active_files_by_size[size]:
                    if active_path not in active_hash_cache:
                        h = get_file_hash(active_path)
                        if h:
                            active_hash_cache[active_path] = h
                    if active_hash_cache.get(active_path) == rec_hash:
                        is_duplicate = True
                        break

                if is_duplicate:
                    # Move to duplicates directory
                    rel = os.path.relpath(path, RECOVERED_ROOT)
                    dst = os.path.join(DUPLICATES_ROOT, rel)
                    try:
                        move_file_or_dir(path, dst)
                        dup_count += 1
                    except Exception as e:
                        log(f"  [dedup error] failed to move {path}: {e}")
                else:
                    unique_count += 1

            # ── Step 5: Clean up empty directories ───────────────────────────────
            log("\nStep 5: Cleaning up empty directories...")
            notifier.notify("Step 5: Cleaning up empty dirs...")
            cleanup_count = 0
            if os.path.isdir(RECOVERED_ROOT):
                for root, dirs, files in os.walk(RECOVERED_ROOT, topdown=False, followlinks=False):
                    if root == RECOVERED_ROOT:
                        continue
                    try:
                        if not os.listdir(root):
                            os.rmdir(root)
                            cleanup_count += 1
                    except Exception:
                        pass
            log(f"  Cleaned {cleanup_count:,} empty directories.")

            # ── Step 6: Separate remaining unmatched lost+found dirs ─────────────
            log("\nStep 6: Moving remaining lost+found dirs to unmatched/...")
            notifier.notify("Step 6: Separating unmatched dirs...")
            os.makedirs(UNMATCHED_ROOT, exist_ok=True)
            unmatched_count = 0
            if os.path.isdir(RECOVERED_ROOT):
                for item in os.listdir(RECOVERED_ROOT):
                    if not item.startswith("lost+found_"):
                        continue
                    src = os.path.join(RECOVERED_ROOT, item)
                    dst = os.path.join(UNMATCHED_ROOT, item)
                    try:
                        if os.path.exists(dst):
                            shutil.rmtree(dst) if os.path.isdir(dst) else os.remove(dst)
                        shutil.move(src, dst)
                        unmatched_count += 1
                    except Exception as e:
                        log(f"  [unmatched move error] {item}: {e}")
            log(f"  Moved {unmatched_count} unmatched entries.")

            # ── Step 7: Reconstruct Cargo registry index cache ───────────────────
            log("\nStep 7: Reconstructing Cargo registry cache tree...")
            notifier.notify("Step 7: Cargo registry cache...")
            cargo_dest  = os.path.join(RECOVERED_ROOT,
                                       "data/cargo/registry/index/"
                                       "index.crates.io-1949cf8c6b5b557f/.cache")
            cargo_moved = 0
            if os.path.isdir(RECOVERED_ROOT):
                for item in os.listdir(RECOVERED_ROOT):
                    if not (len(item) == 2 or item == "3"):
                        continue
                    path = os.path.join(RECOVERED_ROOT, item)
                    if not os.path.isdir(path):
                        continue
                    try:
                        subdirs   = [s for s in os.listdir(path)
                                     if os.path.isdir(os.path.join(path, s))]
                        exp_len   = 2 if len(item) == 2 else 1
                        if subdirs and all(len(s) == exp_len for s in subdirs):
                            os.makedirs(cargo_dest, exist_ok=True)
                            dst = os.path.join(cargo_dest, item)
                            if os.path.exists(dst):
                                shutil.rmtree(dst)
                            shutil.move(path, dst)
                            cargo_moved += 1
                    except Exception as e:
                        log(f"  [cargo error] {item}: {e}")
            log(f"  Moved {cargo_moved} Cargo cache dirs.")

        if getattr(args, "recheck_duplicates", False):
            dup_moved, cleaned_dirs = run_recheck_duplicates(active_files_by_size, active_hash_cache, notifier)
            dup_count = dup_moved
            cleanup_count = cleaned_dirs

        # ── Accumulate & persist stats ───────────────────────────────────────
        run   = {k: 0 for k in _STAT_KEYS}
        run["runs"]                       = 1
        run["symlinks_restored"]          = restored
        run["dirs_squashed"]              = squashed
        run["tombstones_removed"]         = tombstones_removed
        run["symlink_restore_errors"]    = symlink_errors
        run["active_files_scanned"]       = scanned_active
        run["lf_dirs_matched_hash"]       = matched_dirs - struct_count
        run["lf_dirs_matched_structural"] = struct_count
        run["dirs_merged"]                = len(renames_to_execute)
        run["files_checked_dedup"]        = total_rec
        run["duplicates_moved"]           = dup_count
        run["unique_files"]               = unique_count
        run["empty_dirs_cleaned"]         = cleanup_count
        run["unmatched_entries_moved"]    = unmatched_count
        run["cargo_dirs_moved"]           = cargo_moved
        run["corrupted_null_files_removed"] = null_removed

        prev   = load_stats()
        totals = {k: prev[k] + run[k] for k in _STAT_KEYS}
        totals["last_run_completed"] = True
        totals["last_run_archived"]  = False
        save_stats(totals)
        success = True
        history_log("Run completed successfully.")

    except Exception as e:
        log(f"\n❌  FATAL CRASH: {e}")
        import traceback
        log(traceback.format_exc())
        history_log(f"Run crashed: {e}")
    finally:
        try:
            totals = load_stats()
        except Exception:
            pass

        # ── Summary ──────────────────────────────────────────────────────────
        ts_end = datetime.datetime.now().isoformat()
        sep = "=" * 64
        log(f"\n{sep}")
        log(f"Done!  Run #{totals['runs']}   [{ts_end}]")
        log(sep)
        log(f"  Output dir : {RECOVERY_DIR}")
        log(f"  Log        : {LOG_FILE}")
        log(f"  Stats file : {STATS_FILE}")
        log("")
        log("  Step 0 — Symlink restore & squash")
        log(f"    Text-file symlinks restored : {_fmt(restored,          totals['symlinks_restored'])}")
        log(f"    Dir-symlinks squashed       : {_fmt(squashed,          totals['dirs_squashed'])}")
        log(f"    Tombstone files removed     : {_fmt(tombstones_removed,totals['tombstones_removed'])}")
        
        # Highlight symlink errors in the results table
        err_msg = _fmt(symlink_errors, totals.get('symlink_restore_errors', 0))
        if symlink_errors > 0 or totals.get('symlink_restore_errors', 0) > 0:
            log(f" ⚠  Symlink restoration errors  : {err_msg}  <-- ATTENTION (Non-critical failures)")
        else:
            log(f"    Symlink restoration errors  : {err_msg}")

        log("")
        log("  Step 1 — Active filesystem scan")
        log(f"    Files indexed               : {scanned_active:,}  (snapshot)")
        log("")
        log("  Step 2 — Lost+found matching")
        log(f"    Matched by hash             : {_fmt(matched_dirs - struct_count, totals['lf_dirs_matched_hash'])}")
        log(f"    Matched structurally        : {_fmt(struct_count,               totals['lf_dirs_matched_structural'])}")
        if getattr(args, "recheck_unmatched", False):
            still_unmatched = total_lfs - matched_dirs
            pct_matched = (matched_dirs / total_lfs * 100) if total_lfs else 0.0
            log(f"    Rescanned unmatched ratio   : {matched_dirs}/{still_unmatched} ({pct_matched:.1f}% new matches)")
        log("")
        log("  Step 3 — Directory merging")
        log(f"    Directories merged          : {_fmt(len(renames_to_execute), totals['dirs_merged'])}")
        log("")
        log("  Step 4 — Deduplication")
        log(f"    Files checked               : {_fmt(total_rec,    totals['files_checked_dedup'])}")
        log(f"    Duplicates moved            : {_fmt(dup_count,    totals['duplicates_moved'])}")
        log(f"    Unique files remaining      : {_fmt(unique_count, totals['unique_files'])}")
        log("")
        log("  Step 5 — Cleanup")
        log(f"    Empty dirs removed          : {_fmt(cleanup_count, totals['empty_dirs_cleaned'])}")
        log(f"    Corrupted null files removed: {_fmt(null_removed, totals['corrupted_null_files_removed'])}")
        log("")
        log("  Step 6 — Unmatched")
        log(f"    Unmatched dirs moved        : {_fmt(unmatched_count, totals['unmatched_entries_moved'])}")
        log("")
        log("  Step 7 — Cargo registry")
        log(f"    Cargo cache dirs moved      : {_fmt(cargo_moved, totals['cargo_dirs_moved'])}")
        log(sep)

        notifier.notify(
            f"Done! Run #{totals['runs']} ✔  "
            f"Unique: {unique_count:,} | Dups: {dup_count:,} | "
            f"Errors: {totals.get('symlink_restore_errors', 0)}")

        if _log_file:
            _log_file.close()
        recursive_chown(RECOVERY_DIR)

        shutdown_prompt(success, notifier, symlink_errors, totals)

if __name__ == "__main__":
    main()
