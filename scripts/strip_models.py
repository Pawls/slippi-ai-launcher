#!/usr/bin/env python3

import os
import sys

# Run-from-anywhere bootstrap: ``python scripts/strip_models.py`` puts only
# scripts/ on sys.path, so the LAUNCHER package isn't importable without
# this. ``python -m LAUNCHER.api`` (the backend's invocation context) is
# unaffected since it adds the repo root automatically.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from absl import app, flags

from LAUNCHER.checkpoint_ops import strip_one

SRC = flags.DEFINE_string(
    'src', 'pickled_models',
    'Path to the directory containing the models to strip')
DST = flags.DEFINE_string(
    'dst', 'stripped_models',
    'Path to the directory to save the stripped models')
VERBOSE = flags.DEFINE_bool(
    'verbose', False, 'Prints out the models that are stripped')


def needs_copy(src, dst):
  if not os.path.exists(dst):
    return True
  return os.path.getmtime(src) > os.path.getmtime(dst)


def run(src: str, dst: str, verbose: bool = False):
  os.makedirs(dst, exist_ok=True)
  for model in os.listdir(src):
    src_path = os.path.join(src, model)
    dst_path = os.path.join(dst, model)
    if not needs_copy(src_path, dst_path):
      continue
    strip_one(src_path, dst_path)
    if verbose:
      print(f'Stripped {model}')


def main(_):
  run(SRC.value, DST.value, VERBOSE.value)


if __name__ == '__main__':
  app.run(main)
