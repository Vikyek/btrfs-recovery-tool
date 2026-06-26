#!/usr/bin/env python3
"""
Home Directory Declutter and Repo Organizer.
Checks for misplaced Git repositories and empty directories in ~/
- Relocates active repos (with uncommitted changes, user commits, or ahead of upstream) to ~/Projects/
- Deletes redundant clones (no user commits, no uncommitted changes, not ahead of upstream)
- Deletes empty directories
"""
import os
import shutil
import subprocess
import sys
import datetime

# Configuration
HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, "Projects")
LOG_FILE = os.path.join(HOME, "recovery", "declutter.log") if os.path.isdir(os.path.join(HOME, "recovery")) else os.path.join(HOME, "declutter.log")

# Standard folders we should never touch or move
STANDARD_DIRS = {
    "Documents", "Downloads", "Pictures", "Music", "Videos", "Desktop",
    "Templates", "Public", "Projects", "Games", "Games3", "OneDrive",
    "Screenshots", "go", "recovery", "scratch", "Winboat"
}

# Standard hidden folders to ignore entirely
STANDARD_HIDDEN = {
    ".config", ".cache", ".local", ".ssh", ".gnupg", ".mozilla", ".cargo",
    ".rustup", ".npm", ".nvm", ".vscode", ".vscode-oss", ".vscode-oss-shared",
    ".vscode-csharp-dev-tools", ".cursor", ".var", ".gemini", ".gemini_security",
    ".antigravity", ".antigravity_cockpit", ".antigravitycli", ".git",
    ".pki", ".android", ".conda", ".docker", ".kube", ".vim", ".yarn",
    ".subversion", ".fontconfig", ".nv", ".electron-gyp", ".java",
    ".twinny", ".codebuddy", ".copilot", ".minikube", ".mamba", ".gsutil",
    ".cfg" # Bare git config repo
}

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{ts}] {msg}"
    print(formatted)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(formatted + "\n")
    except Exception:
        pass

def run_git_cmd(repo_path, args):
    try:
        res = subprocess.run(
            ["git", "-C", repo_path] + args,
            capture_output=True, text=True, check=True
        )
        return res.stdout.strip()
    except Exception:
        return None

def is_repo_active(repo_path, user_email, user_name):
    # 1. Check for uncommitted (unstaged or staged) changes
    status = run_git_cmd(repo_path, ["status", "--porcelain"])
    if status:
        log(f"  [Repo Check] {os.path.basename(repo_path)}: Has uncommitted changes.")
        return True

    # 2. Check if we are ahead of tracking branch
    ahead = run_git_cmd(repo_path, ["rev-list", "--count", "@{u}..HEAD"])
    if ahead and ahead.isdigit() and int(ahead) > 0:
        log(f"  [Repo Check] {os.path.basename(repo_path)}: Ahead of tracking branch by {ahead} commit(s).")
        return True

    # 3. Check if user has authored any commits
    # Get all commit authors
    authors_out = run_git_cmd(repo_path, ["log", "--format=%ae|%an"])
    if authors_out:
        authors = set(authors_out.splitlines())
        user_matched = False
        for author in authors:
            email, name = (author.split("|") + [""])[:2]
            if (user_email and user_email.lower() in email.lower()) or (user_name and user_name.lower() in name.lower()):
                user_matched = True
                break
        
        # If user is the *only* author, it's fully authored by user
        if user_matched:
            # Check how many commits are by the user
            user_commits_cnt = 0
            commits_out = run_git_cmd(repo_path, ["log", f"--author={user_email or user_name}", "--oneline"])
            if commits_out:
                user_commits_cnt = len(commits_out.splitlines())
            
            log(f"  [Repo Check] {os.path.basename(repo_path)}: User has authored {user_commits_cnt} commit(s).")
            return True
            
    return False

def check_and_delete_empty(dir_path):
    try:
        if not os.listdir(dir_path):
            log(f"Deleting empty directory: {dir_path}")
            os.rmdir(dir_path)
            return True
    except Exception as e:
        log(f"Error checking/deleting directory {dir_path}: {e}")
    return False

def main():
    log("=" * 60)
    log("Declutter Home job started.")
    log("=" * 60)

    # Get git identity configs
    user_email = run_git_cmd(HOME, ["config", "user.email"])
    user_name = run_git_cmd(HOME, ["config", "user.name"])
    
    os.makedirs(PROJECTS_DIR, exist_ok=True)

    # Scan directories in Home
    for name in sorted(os.listdir(HOME)):
        if name in STANDARD_DIRS or name in STANDARD_HIDDEN:
            continue

        path = os.path.join(HOME, name)
        if not os.path.isdir(path) or os.path.islink(path):
            continue

        git_dir = os.path.join(path, ".git")
        if os.path.isdir(git_dir):
            log(f"Found misplaced Git repository: {name}")
            if is_repo_active(path, user_email, user_name):
                # Relocate to Projects
                dst = os.path.join(PROJECTS_DIR, name)
                log(f"  -> Moving active repository to Projects: {dst}")
                try:
                    if os.path.exists(dst):
                        log(f"  [Warning] Destination {dst} already exists. Merging.")
                        shutil.copytree(path, dst, dirs_exist_ok=True)
                        shutil.rmtree(path)
                    else:
                        shutil.move(path, dst)
                except Exception as e:
                    log(f"  Error moving {path} -> {dst}: {e}")
            else:
                # Trash redundant repository
                log(f"  -> Deleting redundant clone: {path}")
                try:
                    shutil.rmtree(path)
                except Exception as e:
                    log(f"  Error deleting redundant clone {path}: {e}")
        else:
            # Check if it's empty
            check_and_delete_empty(path)

    log("Declutter Home job finished.")

if __name__ == "__main__":
    main()
