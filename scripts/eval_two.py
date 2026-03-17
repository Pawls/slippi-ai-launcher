"""Run a game between two trained agents, or vs a human player.

To run two agents against each other:

```shell
python scripts/eval_two.py \
  --dolphin.path=/path/to/slippi-dolphin \
  --dolphin.iso=/path/to/SSBM.iso \
  --p1.ai.path=/path/to/agent1 \
  --p2.ai.path=/path/to/agent2
```

To run an agent against a human player in port 1:

```shell
python scripts/eval_two.py \
  --dolphin.path=/path/to/slippi-dolphin \
  --dolphin.iso=/path/to/SSBM.iso \
  --p1.type=human \
  --p2.ai.path=/path/to/agent
```

"""

import logging
import os

from absl import app
from absl import flags
import fancyflags as ff
import tensorflow as tf

from slippi_ai import eval_lib, flag_utils, utils
from slippi_ai import dolphin as dolphin_lib

PORTS = (1, 2)

player_flags = utils.map_nt(lambda x: x, eval_lib.PLAYER_FLAGS)
player_flags['ai']['async_inference'] = ff.Boolean(True)

PLAYERS = {p: ff.DEFINE_dict(f"p{p}", **player_flags) for p in PORTS}

dolphin_config = dolphin_lib.DolphinConfig(
    headless=False,
    infinite_time=False,
    online_delay=2,
    path=os.environ.get('DOLPHIN_PATH'),
    iso=os.environ.get('ISO_PATH'),
)
DOLPHIN = ff.DEFINE_dict(
    'dolphin', **flag_utils.get_flags_from_default(dolphin_config))

NUM_GAMES = flags.DEFINE_integer('num_games', None, 'Number of games to play')
USE_GPU = flags.DEFINE_boolean('use_gpu', False, 'Use GPU for AI inference. Reduces CPU contention with Dolphin.')

FLAGS = flags.FLAGS

def main(_):
  if USE_GPU.value:
    gpus = tf.config.list_physical_devices('GPU')
    for gpu in gpus:
      tf.config.experimental.set_memory_growth(gpu, True)
    if gpus:
      logging.info(f'Using GPU for inference: {gpus[0].name}')
    else:
      logging.warning(
          '--use_gpu set but no GPU found, falling back to CPU. '
          'TensorFlow %s does not include CUDA support on native Windows '
          '(dropped after TF 2.10). GPU inference requires WSL2 or Linux.',
          tf.__version__)
      eval_lib.disable_gpus()
  else:
    eval_lib.disable_gpus()

  players = {
      port: eval_lib.get_player(**player.value)
      for port, player in PLAYERS.items()
  }

  agents: list[eval_lib.Agent] = []

  for port, opponent_port in zip(PORTS, reversed(PORTS)):
    player = players[port]
    if isinstance(player, dolphin_lib.AI):
      agent = eval_lib.build_agent(
          port=port,
          opponent_port=opponent_port,
          console_delay=DOLPHIN.value['online_delay'],
          **PLAYERS[port].value['ai'],
      )
      agent.start()
      agents.append(agent)

      eval_lib.update_character(player, agent.config)

  # TODO: use an envs.Environment like in RL
  dolphin = dolphin_lib.Dolphin(
      players=players,
      **dolphin_lib.DolphinConfig.kwargs_from_flags(DOLPHIN.value),
  )

  for agent in agents:
    agent.set_controller(dolphin.controllers[agent._port])

  step_timer = utils.Profiler()

  num_games = 0

  # Main loop
  try:
    for gamestate in dolphin.iter_gamestates(skip_menu_frames=False):
      if dolphin_lib.is_menu_state(gamestate):
        if num_games == NUM_GAMES.value:
          break
        continue

      if gamestate.frame == -123: # initial frame
        num_games += 1
        logging.info(f'Game {num_games}')

      with step_timer:
        for agent in agents:
          agent.step(gamestate)

      if gamestate.frame > 0 and gamestate.frame % (15 * 60) == 0:
        logging.info(f'step_time: {step_timer.mean_time():.3f}')
        if agents:
          da = agents[0]._agent
          msg = f'  inference={da.step_profiler.mean_time():.3f}s'
          if hasattr(da, 'state_queue_profiler'):
            msg += f'  queue_wait={da.state_queue_profiler.mean_time():.3f}s'
          logging.info(msg)
          for agent in agents:
            if agent.input_comparisons > 0:
              logging.info(
                  f'  port {agent._port} inputs: {agent.input_mismatches}/{agent.input_comparisons} mismatches')
  finally:
    for agent in agents:
      agent.stop()
    dolphin.stop()

if __name__ == '__main__':
  # https://github.com/python/cpython/issues/87115
  __spec__ = None
  app.run(main)
