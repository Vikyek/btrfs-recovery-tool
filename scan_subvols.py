#!/usr/bin/env python3
"""
Btrfs ROOT_ITEM scanner.
Stage 1: Scans Btrfs raw partitions directly block-by-block for ROOT_ITEMs.
Finds subvolume root block addresses and generations matching a filesystem UUID.
"""
import struct
import argparse
import uuid
import sys
import os

# Default chunk list (standard candidate locations/boundaries for Btrfs chunks)
DEFAULT_CHUNKS = [
    (38797312, 1073741824),
    (144993943552, 1073741824),
    (180427423744, 1073741824),
    (191164841984, 1073741824),
    (336119988224, 1073741824),
    (347931148288, 1073741824),
    (375848435712, 1073741824),
    (49430921216, 1073741824),
    (51578404864, 1073741824),
    (22020096, 8388608),
]

def main():
    parser = argparse.ArgumentParser(description="Scan Btrfs partition block-by-block for subvolume ROOT_ITEMs")
    parser.add_argument("--device", default="/dev/sdc1", help="Device block file (e.g. /dev/sdc1)")
    parser.add_argument("--uuid", default="63531dbd-3d56-4e67-b038-a38b00b85232", help="Btrfs filesystem UUID (string format)")
    parser.add_argument("--chunks", help="Comma-separated offsets:lengths to scan (e.g. '38797312:1073741824,144993943552:1073741824')")
    args = parser.parse_args()

    device = args.device
    if not os.path.exists(device):
        print(f"Error: Device file '{device}' does not exist.")
        sys.exit(1)

    try:
        fs_uuid_expected = uuid.UUID(args.uuid).bytes
    except ValueError:
        print(f"Error: Invalid UUID format '{args.uuid}'. Must be a standard 36-char string.")
        sys.exit(1)

    chunks = DEFAULT_CHUNKS
    if args.chunks:
        chunks = []
        try:
            for pair in args.chunks.split(","):
                off, length = pair.split(":")
                chunks.append((int(off.strip()), int(length.strip())))
        except ValueError:
            print("Error: Chunks parameter must be formatted as offset:length pairs separated by commas.")
            sys.exit(1)

    root_items = []
    print(f"Scanning '{device}' for ROOT_ITEM versions matching UUID {args.uuid}...", flush=True)

    try:
        with open(device, "rb") as f:
            for idx, (offset, length) in enumerate(chunks):
                print(f"  Scanning chunk {idx+1}/{len(chunks)} at offset {offset:,} (len: {length:,})...", flush=True)
                chunk_read = 0
                chunk_step = 64 * 1024 * 1024
                while chunk_read < length:
                    to_read = min(chunk_step, length - chunk_read)
                    f.seek(offset + chunk_read)
                    buf = f.read(to_read)
                    if not buf:
                        break
                    
                    block_step = 16384
                    for block_offset in range(0, len(buf), block_step):
                        block = buf[block_offset:block_offset + block_step]
                        if len(block) < 16384:
                            continue
                        
                        fs_uuid = block[32:48]
                        if fs_uuid != fs_uuid_expected:
                            continue
                        
                        bytenr = struct.unpack("<Q", block[48:56])[0]
                        generation = struct.unpack("<Q", block[80:88])[0]
                        owner = struct.unpack("<Q", block[88:96])[0]
                        nritems = struct.unpack("<I", block[96:100])[0]
                        level = block[100]
                        
                        if level != 0:
                            continue
                        
                        for i in range(nritems):
                            item_offset = 101 + i * 25
                            if item_offset + 25 > 16384:
                                break
                            
                            item_buf = block[item_offset:item_offset + 25]
                            objectid = struct.unpack("<Q", item_buf[0:8])[0]
                            key_type = item_buf[8]
                            key_offset = struct.unpack("<Q", item_buf[9:17])[0]
                            data_offset = struct.unpack("<I", item_buf[17:21])[0]
                            data_size = struct.unpack("<I", item_buf[21:25])[0]
                            
                            if key_type == 132: # RootItem
                                abs_data_offset = 101 + data_offset
                                if abs_data_offset + data_size <= 16384:
                                    data_buf = block[abs_data_offset:abs_data_offset + data_size]
                                    if len(data_buf) >= 184:
                                        subvol_bytenr = struct.unpack("<Q", data_buf[176:184])[0]
                                        subvol_gen = struct.unpack("<Q", data_buf[160:168])[0]
                                        root_items.append((objectid, subvol_bytenr, subvol_gen, generation, bytenr))
                    
                    chunk_read += to_read
    except PermissionError:
        print(f"Error: Permission denied accessing '{device}'. Please run with sudo.")
        sys.exit(1)
    except Exception as e:
        print(f"Fatal error reading device: {e}")
        sys.exit(1)

    print("\nAll found ROOT_ITEMS:")
    # Filter standard user subvolumes (typically >= 256)
    filtered = [r for r in root_items if 256 <= r[0] <= 265]
    if not filtered:
        print("  No ROOT_ITEMs found in requested range.")
        return

    for objectid, subvol_bytenr, subvol_gen, root_tree_gen, root_tree_bytenr in sorted(filtered, key=lambda x: (x[0], x[2])):
        name = "@home" if objectid == 259 else ("@" if objectid == 258 else f"subvol_{objectid}")
        print(f"Subvol: {name} ({objectid}) | Subvol Root Block: {subvol_bytenr} | Subvol Gen: {subvol_gen} | ROOT_TREE Gen: {root_tree_gen} | ROOT_TREE Block: {root_tree_bytenr}")

if __name__ == "__main__":
    main()
