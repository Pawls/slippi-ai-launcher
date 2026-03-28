"""Prepare parsing in the local filesystem.

Accepts a source directory containing a mixture of:
  - Subdirectories of .slp files (will be archived into .7z)
  - Existing .7z or .zip archives (will be copied directly)

Source structure (any combination):
.
├── 2024-04
│   ├── Game_20250404T110624.slp
│   └── ... more slp files
├── 2024-05.7z
├── 2024-06.zip
└── ... more directories or archives

Output structure (ready for parse_local.py):
.
├── Parsed
└── Raw
    ├── 2024-04.7z
    ├── 2024-05.7z
    ├── 2024-06.zip
    └── ... more archives

Existing archives in Raw/ are skipped to avoid duplicate work.
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

def run_preparation(source, dest):
    if not os.path.isabs(source):
        source = os.path.join(os.getcwd(), source)
    if not os.path.isabs(dest):
        dest = os.path.join(os.getcwd(), dest)

    validate_source_directory(source)
    create_destination_directory(dest)

    raw_dir = os.path.join(dest, 'Raw')

    # Separate source contents into directories and archive files
    directories = []
    archives = []
    needs_7z = False

    for entry in sorted(os.listdir(source)):
        full_path = os.path.join(source, entry)
        if os.path.isdir(full_path):
            directories.append(entry)
            needs_7z = True
        elif entry.lower().endswith(('.7z', '.zip')):
            archives.append(entry)

    if needs_7z and not seven_zip_exists_in_path():
        raise Exception('Couldn\'t find 7z in path, install it for your platform')

    if not directories and not archives:
        print(f'No directories or archives found in {source}, no work to be done.')
        return

    # Copy existing archives directly into Raw/
    for archive in archives:
        src_path = os.path.join(source, archive)
        dest_path = os.path.join(raw_dir, archive)

        if os.path.exists(dest_path):
            print(f'SKIPPING: {archive} already exists in Raw/')
            continue

        print(f'COPYING: {archive} -> Raw/')
        shutil.copy2(src_path, dest_path)

    # Archive loose .slp directories into 7z
    for d in directories:
        source_dir = os.path.join(source, d)
        destination_archive = os.path.join(raw_dir, f'{d}.7z')

        if os.path.exists(destination_archive):
            print(f'SKIPPING: {d}.7z already exists in Raw/')
            continue

        print(f'ARCHIVING: {source_dir} -> {d}.7z')

        command = f'7z a -t7z -mx=5 "{destination_archive}" "{source_dir}"'
        process = subprocess.run(command, shell=True, capture_output=True, text=True)
        if process.returncode != 0:
            print(f'WARNING: failed to create 7z archive for: {d}')
            print(process.stderr)

    print('Done.')

def main(_):
    run_preparation(FLAGS.slp_root, FLAGS.zip_root)

if __name__ == '__main__':
    SLP_ROOT = flags.DEFINE_string('slp_root',
        None,
        'root directory containing slippi files',
        required=True)

    ZIP_ROOT = flags.DEFINE_string('zip_root',
        None,
        'destination root directory where the archives will be placed',
        required=True)

    app.run(main)
