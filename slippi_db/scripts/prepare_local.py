"""Prepare parsing in the local filesystem.

Accepts a source directory containing a mixture of:
  - Subdirectories (at any depth) containing .slp files (archived into .7z)
  - Existing .7z or .zip archives (copied directly)

Source structure (any combination, any nesting):
.
├── 2024-04
│   ├── Game_20250404T110624.slp
│   └── ... more slp files
├── Ranked
│   └── 2024-05
│       └── Game_20250501T120000.slp
├── 2024-06.7z
├── 2024-07.zip
└── ... more directories or archives

Output structure (ready for parse_local.py):
.
├── Parsed
└── Raw
    ├── 2024-04.7z
    ├── Ranked-2024-05.7z
    ├── 2024-06.7z
    ├── 2024-07.zip
    └── ... more archives

Existing archives in Raw/ are skipped to avoid duplicate work.
Archive names for nested directories use dashes: parent-child.7z
"""

import os
import shutil
import subprocess

from absl import app, flags

FLAGS = flags.FLAGS


def seven_zip_exists_in_path():
    path = shutil.which("7z")
    return path is not None

def validate_source_directory(source):
    if not os.path.exists(source):
        raise ValueError(f'Failed to find a directory at {source}')

    if not os.path.isdir(source):
        raise ValueError(f'Found something at {source} but it is not a directory')

def create_destination_directory(dest):
    raw_dir = os.path.join(dest, 'Raw')
    parsed_dir = os.path.join(dest, 'Parsed')

    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(parsed_dir, exist_ok=True)

def _has_slp_files(directory):
    """Check if a directory directly contains any .slp files."""
    try:
        return any(f.lower().endswith('.slp') for f in os.listdir(directory)
                   if os.path.isfile(os.path.join(directory, f)))
    except OSError:
        return False

def run_preparation(source, dest):
    if not os.path.isabs(source):
        source = os.path.join(os.getcwd(), source)
    if not os.path.isabs(dest):
        dest = os.path.join(os.getcwd(), dest)

    validate_source_directory(source)
    create_destination_directory(dest)

    raw_dir = os.path.join(dest, 'Raw')

    # Walk the source tree to find:
    # 1. Archive files (.7z, .zip) at any depth
    # 2. Directories that directly contain .slp files
    archives = []   # (full_path, archive_filename)
    slp_dirs = []   # (full_path, archive_name)
    needs_7z = False

    for dirpath, dirnames, filenames in os.walk(source):
        # Collect archive files
        for f in filenames:
            if f.lower().endswith(('.7z', '.zip')):
                archives.append((os.path.join(dirpath, f), f))

        # Check if this directory directly contains .slp files
        has_slp = any(f.lower().endswith('.slp') for f in filenames)
        if has_slp:
            # Build archive name from relative path (e.g. "Ranked/2024-05" -> "Ranked-2024-05")
            rel = os.path.relpath(dirpath, source)
            if rel == '.':
                # .slp files in the source root itself - use source dir name
                archive_name = os.path.basename(source)
            else:
                archive_name = rel.replace(os.sep, '-')
            slp_dirs.append((dirpath, archive_name))
            needs_7z = True

    if needs_7z and not seven_zip_exists_in_path():
        raise Exception('Couldn\'t find 7z in path, install it for your platform')

    if not slp_dirs and not archives:
        print(f'No .slp directories or archives found in {source}, no work to be done.')
        return

    # Copy existing archives directly into Raw/
    for src_path, filename in sorted(archives):
        dest_path = os.path.join(raw_dir, filename)

        if os.path.exists(dest_path):
            print(f'SKIPPING: {filename} already exists in Raw/')
            continue

        print(f'COPYING: {filename} -> Raw/')
        shutil.copy2(src_path, dest_path)

    # Archive directories containing .slp files
    for dirpath, archive_name in sorted(slp_dirs):
        destination_archive = os.path.join(raw_dir, f'{archive_name}.7z')

        if os.path.exists(destination_archive):
            print(f'SKIPPING: {archive_name}.7z already exists in Raw/')
            continue

        print(f'ARCHIVING: {dirpath} -> {archive_name}.7z')

        command = f'7z a -t7z -mx=5 "{destination_archive}" "{dirpath}"'
        process = subprocess.run(command, shell=True, capture_output=True, text=True)
        if process.returncode != 0:
            print(f'WARNING: failed to create 7z archive for: {archive_name}')
            print(process.stderr)

    print('Done.')

def main(_):
    sources = list(FLAGS.slp_root)
    multi = len(sources) > 1
    for i, slp_root in enumerate(sources, start=1):
        if multi:
            print(f'\n=== [{i}/{len(sources)}] Preparing {slp_root} ===')
        run_preparation(slp_root, FLAGS.zip_root)

if __name__ == '__main__':
    SLP_ROOT = flags.DEFINE_multi_string('slp_root',
        None,
        'root directory containing slippi files; may be repeated to '
        'process multiple source directories into the same output',
        required=True)

    ZIP_ROOT = flags.DEFINE_string('zip_root',
        None,
        'destination root directory where the archives will be placed',
        required=True)

    app.run(main)
