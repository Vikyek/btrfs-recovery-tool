# Btrfs Recovery & Merging Toolset

A robust, three-stage Python toolset designed to scan corrupted Btrfs block devices, extract lost files/subvolumes, verify and deduplicate them against the active filesystem, and safely merge recovered files back into the active system without data loss or overwrites.

---

## 🛠️ Workflow Stages

```mermaid
graph TD
    A[Stage 1: scan_subvols.py] -->|Discovers Root Blocks / Gens| B[Run btrfs restore]
    B -->|Restores files to recovery dir| C[Stage 2: clean_recovery.py]
    C -->|Verifies, deduplicates, and resolves symlinks| D[Stage 3: clean_file_merge.py (via clean-file-merge)]
    D -->|Safely merges back| E[Active Filesystem]
```

### 1. Stage 1 — Direct ROOT_ITEM Scanning (`scan_subvols.py`)
This script directly reads a Btrfs raw partition block-by-block. It scans candidate chunk offset ranges for metadata trees containing Btrfs `ROOT_ITEM` structures matching a specific filesystem UUID.
* **Output:** Discovers valid subvolume root block addresses and generation timestamps. These blocks can be fed directly to the `btrfs restore -t <block>` command to extract files from specific subvolumes and generations.

### 2. Stage 2 — Symlink Resolution & Deduplication (`clean_recovery.py`)
After files are restored into a recovery folder (default `recreated_dir_tree`), this stage refines the data by checking it against the active main filesystem:
* **Symlink Correction:** Restores corrupted text-file symlinks and squashes matching directory symlinks to avoid circular walks.
* **Deduplication:** Computes file sizes and SHA-256 hashes to match recovered files against files currently in the active directory. Identical files are moved to `duplicates/` to keep the recovery tree clean.
* **Lost+Found Resolution:** Matches orphan folders inside `lost+found_xxxx` directories based on structural file hierarchies and content hashes, restoring them to their correct relative paths.
* **Unmatched Separation:** Moves unmatched remaining `lost+found_` directories into `unmatched/` for isolation.
* **Cargo Cache Registry Cache Reconstruction:** Rebuilds Cargo's dependency index caches to clean up package data.

### 3. Stage 3 — Main Filesystem Merging (`clean_file_merge.py` via `clean-file-merge`)
Recursively merges the final cleaned recovery folder back into the active main filesystem:
* **Safety First:** Moves all files/directories atomically where possible. It **never overwrites** existing active files; collisions are logged and skipped.
* **Broken Symlink Replacement:** Replaces target broken symlinks in the active filesystem with the recovered folder or files.

---

## 🚀 How to Run

### Requirements
* Python 3.x
* Root permissions (`sudo`) for raw device reads and file merging.

### Stage 1: Discover Subvolumes & Generations
Scan a partition for subvolumes matching a filesystem UUID:
```bash
sudo python3 scan_subvols.py --device /dev/sdX1 --uuid 12345678-abcd-1234-abcd-1234567890ab
```

### Intermediate Step: Run Btrfs Restore
Use the block numbers output by Stage 1 to restore files to a recovery directory:
```bash
sudo btrfs restore -r 259 -t <discovered_block_number> /dev/sdX1 /home/user/recovery/recreated_dir_tree
```

### Stage 2: Verify & Clean Recovery
Scan and deduplicate the recovered files against the active filesystem:
```bash
sudo python3 clean_recovery.py \
  --active-root /home/user \
  --recovery-dir /home/user/recovery \
  --recreated-dir-name recreated_dir_tree \
  --merge y
```
* **Interactive Prompts Bypass:** Use `--resume y --archive n --wipe n` flags to run the script entirely in non-interactive batch mode.
* **Rechecking Unmatched Files:** Run `clean_recovery.py --recheck-unmatched` to quickly recheck unmatched `lost+found_` files from previous runs against the active disk.
* **Rechecking Leftover Duplicates:** Run `clean_recovery.py --recheck-duplicates` to quickly scan all recovery directories, match remaining duplicates, and move them to `duplicates/`.

### Stage 3: Merge back to Active Directory
If run manually, merge the clean recovery directory back:
```bash
sudo clean-file-merge --src /home/user/recovery/recreated_dir_tree --dst /home/user
```

---

## 📋 Command-Line Arguments Help

### `clean_recovery.py`
```
options:
  -h, --help            show this help message and exit
  --active-root PATH    Path to main active filesystem directory (default: ~/ or current user home)
  --recovery-dir PATH   Path to base recovery directory (default: ~/recovery)
  --recreated-dir-name NAME
                        Folder name of recreated directory tree under recovery-dir (default: recreated_dir_tree)
  --duplicates-dir-name NAME
                        Folder name of duplicates under recovery-dir (default: duplicates)
  --unmatched-dir-name NAME
                        Folder name of unmatched under recovery-dir (default: unmatched)
  --resume {y,n,a}      Answer for resume prompt if unfinished run is detected
  --archive {y,n}       Answer for archiving prompt (at startup or shutdown)
  --wipe {y,n}          Answer for wiping leftovers prompt
  --recheck-unmatched   Only recheck unmatched files in unmatched/ directory from the last run
  --recheck-duplicates  Scan recovery directories for files that duplicate active files and move them to duplicates/
  --merge {y,n}         Answer for automatically running recovery merge prompt
  --on-symlink {ask,preserve,clear,archive,skip}
                        Handling of working symlink collisions during recovery merge (default: ask)
```

### Logging & Archives
- **Logs**: Run logs are written to `~/recovery/clean_recovery.log` (persistent history in `~/recovery/history.log`).
- **Cross-Logs**: Symlinks to `clean-file-merge` sessions are created in `~/recovery/cfm_session_NNN.log`.
- **Archives**: Gzip archives of completed recovery runs are saved in `~/Backups/btrfs-recovery/run_<label>_<timestamp>.tar.gz` and include squashed log files.

### `clean-file-merge` (Dependency)
See the [clean-file-merge](file:///home/v/Projects/clean-file-merge) repository for details on command line options and features.

### `scan_subvols.py`
```
options:
  -h, --help            show this help message and exit
  --device DEV          Device block file (default: /dev/sdX1)
  --uuid UUID           Btrfs filesystem UUID (Required)
  --chunks CHUNKS       Comma-separated offsets:lengths to scan (e.g. 'offset:length,offset:length')
```

## 📄 License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.



