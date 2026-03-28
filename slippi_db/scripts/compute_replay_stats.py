"""Compute gameplay statistics for parsed replays and store in SQLite.

Reads parquet files from Parsed/, computes per-player stats using
slippi_ai.replay_stats, and stores results in the replay_stats table
of parsed.sqlite. Incremental: skips replays already computed.

Usage:
  python slippi_db/scripts/compute_replay_stats.py --root=Root [--threads=4] [--recompute]
"""

import os
import sqlite3
import time
from datetime import datetime, timezone

from absl import app, flags

from slippi_ai.data import read_table
from slippi_ai.replay_stats import compute_replay_stats, STAT_COLUMNS

FLAGS = flags.FLAGS

ROOT = flags.DEFINE_string('root', None, 'Dataset root directory', required=True)
THREADS = flags.DEFINE_integer('threads', 1, 'Number of worker threads')
RECOMPUTE = flags.DEFINE_boolean('recompute', False, 'Recompute all stats')

STATS_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS replay_stats (
    slp_md5 TEXT NOT NULL,
    player_index INTEGER NOT NULL,
    {', '.join(f'{col} REAL' for col in STAT_COLUMNS)},
    computed_at TEXT,
    PRIMARY KEY (slp_md5, player_index)
);
"""

# Fix integer columns (REAL is fine for SQLite, but let's be explicit)
STATS_SCHEMA = STATS_SCHEMA.replace(
  'l_cancel_attempts REAL', 'l_cancel_attempts INTEGER'
).replace(
  'kill_count REAL', 'kill_count INTEGER'
).replace(
  'death_count REAL', 'death_count INTEGER'
).replace(
  'conversion_count REAL', 'conversion_count INTEGER'
).replace(
  'roll_count REAL', 'roll_count INTEGER'
).replace(
  'spotdodge_count REAL', 'spotdodge_count INTEGER'
).replace(
  'airdodge_count REAL', 'airdodge_count INTEGER'
).replace(
  'ledgegrab_count REAL', 'ledgegrab_count INTEGER'
).replace(
  'wavedash_count REAL', 'wavedash_count INTEGER'
).replace(
  'dash_dance_count REAL', 'dash_dance_count INTEGER'
).replace(
  'num_frames REAL', 'num_frames INTEGER'
)

INSERT_SQL = f"""
INSERT OR REPLACE INTO replay_stats (
    slp_md5, player_index, {', '.join(STAT_COLUMNS)}, computed_at
) VALUES (
    ?, ?, {', '.join('?' for _ in STAT_COLUMNS)}, ?
)
"""


def get_training_replays(parsed_db: str) -> list[dict]:
  """Query parsed.sqlite for training replays with their compression type."""
  conn = sqlite3.connect(f'file:{parsed_db}?mode=ro', uri=True)
  conn.row_factory = sqlite3.Row
  rows = conn.execute(
    "SELECT slp_md5, compression FROM replays WHERE is_training = 1"
  ).fetchall()
  conn.close()
  return [dict(r) for r in rows]


def get_existing_md5s(parsed_db: str) -> set[str]:
  """Get set of slp_md5 values already computed."""
  conn = sqlite3.connect(f'file:{parsed_db}?mode=ro', uri=True)
  try:
    rows = conn.execute(
      "SELECT DISTINCT slp_md5 FROM replay_stats"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}
  except sqlite3.OperationalError:
    # Table doesn't exist yet
    conn.close()
    return set()


def process_one(parsed_dir: str, slp_md5: str, compression: str) -> list[tuple] | None:
  """Process a single replay file and return insert tuples."""
  pq_path = os.path.join(parsed_dir, slp_md5)
  if not os.path.isfile(pq_path):
    return None

  compressed = (compression == 'zlib')
  try:
    game = read_table(pq_path, compressed=compressed)
  except Exception as e:
    print(f'  WARNING: Failed to read {slp_md5}: {e}')
    return None

  try:
    stats = compute_replay_stats(game)
  except Exception as e:
    print(f'  WARNING: Failed to compute stats for {slp_md5}: {e}')
    return None

  now = datetime.now(timezone.utc).isoformat()
  tuples = []
  for player_idx in (0, 1):
    prefix = f'p{player_idx}_'
    values = [stats[f'{prefix}{col}'] for col in STAT_COLUMNS]
    tuples.append((slp_md5, player_idx, *values, now))

  return tuples


def main(_):
  root = ROOT.value
  parsed_db = os.path.join(root, 'parsed.sqlite')
  parsed_dir = os.path.join(root, 'Parsed')

  if not os.path.isfile(parsed_db):
    print(f'ERROR: parsed.sqlite not found at {parsed_db}')
    print('Run parse_local.py first.')
    return

  if not os.path.isdir(parsed_dir):
    print(f'ERROR: Parsed/ directory not found at {parsed_dir}')
    return

  # Create stats table
  conn = sqlite3.connect(parsed_db)
  conn.execute(STATS_SCHEMA)
  conn.commit()
  conn.close()

  # Get replays to process
  replays = get_training_replays(parsed_db)
  print(f'Found {len(replays)} training replays.')

  if not RECOMPUTE.value:
    existing = get_existing_md5s(parsed_db)
    replays = [r for r in replays if r['slp_md5'] not in existing]
    print(f'Skipping {len(existing)} already computed. {len(replays)} remaining.')

  if not replays:
    print('Nothing to do.')
    return

  # Process replays
  conn = sqlite3.connect(parsed_db)
  batch = []
  start_time = time.time()
  processed = 0
  errors = 0

  for i, replay in enumerate(replays):
    slp_md5 = replay['slp_md5']
    compression = replay.get('compression', 'zlib')

    tuples = process_one(parsed_dir, slp_md5, compression)
    if tuples is None:
      errors += 1
      continue

    batch.extend(tuples)
    processed += 1

    # Commit in batches of 100
    if len(batch) >= 200:  # 2 rows per replay
      conn.executemany(INSERT_SQL, batch)
      conn.commit()
      batch.clear()

    if (i + 1) % 100 == 0:
      elapsed = time.time() - start_time
      rate = (i + 1) / elapsed
      remaining = (len(replays) - i - 1) / rate if rate > 0 else 0
      print(f'  [{i + 1}/{len(replays)}] {rate:.1f} replays/sec, '
            f'~{remaining:.0f}s remaining')

  # Final commit
  if batch:
    conn.executemany(INSERT_SQL, batch)
    conn.commit()

  conn.close()

  elapsed = time.time() - start_time
  print(f'Done. Processed {processed} replays in {elapsed:.1f}s '
        f'({errors} errors).')


if __name__ == '__main__':
  app.run(main)
