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

from absl import app, flags

from slippi_db import archive_utils

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

def run_preparation(source, dest, archive_format='zip'):
    if archive_format not in archive_utils.ARCHIVE_FORMATS:
        raise ValueError(f'Unsupported archive_format: {archive_format!r}')

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

    # Existing archives that don't match the target format also need 7z to
    # transcode, not just slp_dirs being archived from scratch.
    if archives and not seven_zip_exists_in_path():
        for _, filename in archives:
            ext = os.path.splitext(filename)[1].lower().lstrip('.')
            if ext != archive_format:
                needs_7z = True
                break

    if needs_7z and not seven_zip_exists_in_path():
        raise Exception('Couldn\'t find 7z in path, install it for your platform')

    if not slp_dirs and not archives:
        print(f'No .slp directories or archives found in {source}, no work to be done.')
        return

    target_ext = f'.{archive_format}'

    # Copy or transcode existing archives into Raw/.
    # If the source archive already matches the target format, copy it
    # through. Otherwise re-encode it so the upgrade step (which only
    # reads .zip) doesn't silently skip it.
    for src_path, filename in sorted(archives):
        src_ext = os.path.splitext(filename)[1].lower()
        if src_ext == target_ext:
            dest_filename = filename
        else:
            stem = os.path.splitext(filename)[0]
            dest_filename = f'{stem}{target_ext}'

        dest_path = os.path.join(raw_dir, dest_filename)
        if os.path.exists(dest_path):
            print(f'SKIPPING: {dest_filename} already exists in Raw/')
            continue

        if src_ext == target_ext:
            print(f'COPYING: {filename} -> Raw/')
            tmp_path = dest_path + '.tmp'
            shutil.copy2(src_path, tmp_path)
            os.replace(tmp_path, dest_path)
        else:
            print(f'TRANSCODING: {filename} -> {dest_filename}')
            try:
                archive_utils.transcode_archive(
                    src_path, dest_path, target_format=archive_format)
            except Exception as e:
                print(f'WARNING: failed to transcode {filename}: {e}')

    # Archive directories containing .slp files in the requested format.
    for dirpath, archive_name in sorted(slp_dirs):
        dest_filename = f'{archive_name}{target_ext}'
        destination_archive = os.path.join(raw_dir, dest_filename)

        if os.path.exists(destination_archive):
            print(f'SKIPPING: {dest_filename} already exists in Raw/')
            continue

        print(f'ARCHIVING: {dirpath} -> {dest_filename}')
        try:
            archive_utils.archive_directory(
                dirpath, destination_archive, archive_format=archive_format)
        except Exception as e:
            print(f'WARNING: failed to create {archive_format} archive for {archive_name}: {e}')

    print('Done.')

def main(_):
    sources = list(FLAGS.slp_root)
    multi = len(sources) > 1
    for i, slp_root in enumerate(sources, start=1):
        if multi:
            print(f'\n=== [{i}/{len(sources)}] Preparing {slp_root} ===')
        run_preparation(slp_root, FLAGS.zip_root, archive_format=FLAGS.archive_format)

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

    ARCHIVE_FORMAT = flags.DEFINE_enum('archive_format',
        'zip',
        list(archive_utils.ARCHIVE_FORMATS),
        'Output archive format. Defaults to zip because the upgrade step '
        'and slippi-ai parser only read .zip archives. Use 7z only if you '
        'do not plan to upgrade and want better compression.')

    app.run(main)
