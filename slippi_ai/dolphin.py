import abc
import atexit
import configparser
import dataclasses
import logging
import os
import re
from typing import Dict, Mapping, Optional, Iterator

import fancyflags as ff
import portpicker

import melee
from melee.console import get_dolphin_version, DumpConfig, DolphinBuild, default_dolphin_install_path

class Player(abc.ABC):

  @abc.abstractmethod
  def controller_type(self) -> melee.ControllerType:
    pass

  @abc.abstractmethod
  def menuing_kwargs(self) -> Dict:
    pass


class Human(Player):

  def controller_type(self) -> melee.ControllerType:
    return melee.ControllerType.GCN_ADAPTER

  def menuing_kwargs(self) -> Dict:
    return {}

@dataclasses.dataclass
class CPU(Player):
  character: melee.Character = melee.Character.FOX
  level: int = 9

  def controller_type(self) -> melee.ControllerType:
    return melee.ControllerType.STANDARD

  def menuing_kwargs(self) -> Dict:
    return dict(character_selected=self.character, cpu_level=self.level)

@dataclasses.dataclass
class AI(Player):
  character: melee.Character = melee.Character.FOX

  def controller_type(self) -> melee.ControllerType:
    return melee.ControllerType.STANDARD

  def menuing_kwargs(self) -> Dict:
    return dict(character_selected=self.character)

def is_menu_state(gamestate: melee.GameState) -> bool:
  return gamestate.menu_state not in [melee.Menu.IN_GAME, melee.Menu.SUDDEN_DEATH]

def is_game_state(gamestate: melee.GameState) -> bool:
  return gamestate.menu_state in (melee.Menu.IN_GAME, melee.Menu.SUDDEN_DEATH)

INITIAL_FRAME = -123

class ConnectFailed(Exception):
  """Raised when we fail to connect to the console."""

class WrongCharacterSelected(Exception):
  """Raised on the initial frame if the wrong character is selected."""

@dataclasses.dataclass
class GeckoCode:
  """A custom gecko code to inject into the GALE01r2.ini."""
  name: str  # e.g. "No Music"
  code: str  # e.g. "04B664F0 00000000"
  author: str = ''  # e.g. "Dan Salvato"

  @property
  def header(self) -> str:
    if self.author:
      return f'$Optional: {self.name} [{self.author}]'
    return f'$Optional: {self.name}'

  @property
  def enabled_line(self) -> str:
    return f'$Optional: {self.name}'

  @staticmethod
  def parse_ini(text: str) -> list['GeckoCode']:
    """Parse gecko codes from INI-style text.

    Expected format (same as GALE01r2.ini [Gecko] section):
      $Code Name [Author]
      XXXXXXXX XXXXXXXX
      XXXXXXXX XXXXXXXX

      $Another Code
      XXXXXXXX XXXXXXXX
    """
    codes: list[GeckoCode] = []
    name = ''
    author = ''
    code_lines: list[str] = []

    header_re = re.compile(r'^\$(.+?)(?:\s*\[(.+?)\])?\s*$')

    def flush():
      if name and code_lines:
        codes.append(GeckoCode(
            name=name,
            code='\n'.join(code_lines),
            author=author,
        ))

    for raw_line in text.splitlines():
      line = raw_line.strip()
      if not line or line.startswith('*'):
        continue
      m = header_re.match(line)
      if m:
        flush()
        name = m.group(1).strip()
        author = m.group(2).strip() if m.group(2) else ''
        code_lines = []
      elif name:
        code_lines.append(line)

    flush()
    return codes

  @staticmethod
  def load_file(path: str) -> list['GeckoCode']:
    """Load gecko codes from a text file in INI format."""
    with open(path) as f:
      return GeckoCode.parse_ini(f.read())

# Built-in gecko codes
NO_MUSIC = GeckoCode(
    name='No Music',
    author='Dan Salvato',
    code='04B664F0 00000000',
)

class Dolphin:

  def __init__(
      self,
      path: str,
      iso: str,
      players: Mapping[int, Player],
      stage: melee.Stage = melee.Stage.FINAL_DESTINATION,
      online_delay: int = 0,  # overrides Console's default of 2
      blocking_input: bool = True,
      console_timeout: Optional[float] = None,
      slippi_port: Optional[int] = None,  # Picked automatically if None
      save_replays=False,  # Override default in Console
      env_vars: Optional[dict] = None,
      headless: bool = False,
      render: Optional[bool] = None,  # Render even when running headless.
      connect_code: Optional[str] = None,
      copy_home_directory: bool = False,
      min_slp_version: Optional[tuple[int, int, int]] = (3, 18, 0),
      gecko_codes: Optional[list[GeckoCode]] = None,
      gecko_codes_file: Optional[str] = None,
      netplay_port: Optional[int] = None,
      lan_ip: Optional[str] = None,
      **console_kwargs,
  ) -> None:
    self._players = players
    self.stage = stage
    self.min_slp_version = min_slp_version

    platform = None

    # TODO: some of this logic should be moved to Console
    path = path or default_dolphin_install_path()[0]

    # For custom dolphin directories that libmelee doesn't recognize
    # (e.g. naming conventions that don't match standard Slippi builds),
    # resolve to the exe so get_exe_path's naming check is bypassed.
    try:
      version = get_dolphin_version(path)
    except (ValueError, FileNotFoundError):
      if os.path.isdir(path):
        for f in os.listdir(path):
          if 'dolphin' in f.lower() and (
              f.lower().endswith('.exe') or f.lower().endswith('.appimage')):
            path = os.path.join(path, f)
            version = get_dolphin_version(path)
            break
        else:
          raise
      else:
        raise

    if render is None:
      render = not headless

    if not render:
      console_kwargs.update(gfx_backend='Null')

    if headless:
      console_kwargs.update(
          disable_audio=True,
      )
      if version.mainline:
        platform = 'headless'
        # console_kwargs.update(emulation_speed=0)

      if version.build is DolphinBuild.EXI_AI:
        console_kwargs.update(
            use_exi_inputs=True,
            enable_ffw=True,
        )
      elif not version.mainline:
        raise ValueError(
            'Headless requires mainline dolphin or a custom dolphin build. '
            'See https://github.com/vladfi1/libmelee?tab=readme-ov-file#setup-instructions')

    # When path points to an exe (e.g. from dolphin override), Console's
    # _default_home_path will fail because it expects a directory.  Use the
    # standard Slippi installation's home so copy_home_directory picks up the
    # user's real settings/login rather than the custom build's empty User/.
    if os.path.isfile(path):
      from melee.console import default_dolphin_info
      console_kwargs.setdefault(
          'dolphin_home_path', default_dolphin_info().home_path)

    slippi_port = slippi_port or portpicker.pick_unused_port()

    self.menu_helper = melee.MenuHelper()

    console = melee.Console(
        path=path,
        online_delay=online_delay,
        blocking_input=blocking_input,
        polling_mode=console_timeout is not None,
        polling_timeout=console_timeout,
        slippi_port=slippi_port,
        copy_home_directory=copy_home_directory,
        setup_gecko_codes=True,
        save_replays=save_replays,
        **console_kwargs,
    )
    atexit.register(console.stop)
    self.console = console

    all_gecko_codes = list(gecko_codes or [])
    if gecko_codes_file:
      all_gecko_codes.extend(GeckoCode.load_file(gecko_codes_file))
    if all_gecko_codes:
      self._inject_gecko_codes(console, all_gecko_codes)

    if netplay_port is not None or lan_ip is not None:
      self._inject_netplay_settings(console, netplay_port, lan_ip)

    self.controllers: Mapping[int, melee.Controller] = {}
    self._menuing_controllers: list[tuple[melee.Controller, CPU | AI]] = []
    self._autostart = True
    self._connect_code = connect_code

    for port, player in players.items():
      skip_controller = False

      if isinstance(player, Human):
        self._autostart = False
        # Don't overwrite user's controller config
        if copy_home_directory:
          skip_controller = True

      if skip_controller:
        continue

      controller = melee.Controller(
          console, port, player.controller_type())
      self.controllers[port] = controller

      if isinstance(player, (CPU, AI)):
        self._menuing_controllers.append((controller, player))

    console.run(
        iso_path=iso,
        environment_vars=env_vars,
        platform=platform,
    )

    logging.info('Connecting to console...')
    if not console.connect():
      logging.error(
          f"PID {os.getpid()}: failed to connect to the console"
          f" {console.temp_dir} on port {slippi_port}")

      raise ConnectFailed(f"Failed to connect to the console on port {slippi_port}.")
    logging.info('Connected to console')

    for controller in self.controllers.values():
      if controller._type is not melee.ControllerType.STANDARD:
        continue
      if not controller.connect():
        self.stop()
        raise ConnectFailed("Failed to connect the controller.")

  def next_gamestate(self) -> melee.GameState:
    gamestate = self.console.step()
    if gamestate is None:
      raise TimeoutError('Console timed out.')

    # Perform some checks at the start of the game
    if is_game_state(gamestate) and gamestate.frame == INITIAL_FRAME:
      assert self.console.slp_version_tuple is not None

      if (
        self.min_slp_version is not None
        and self.console.slp_version_tuple < self.min_slp_version
      ):
        raise RuntimeError(
          f'Slippi version {self.console.slp_version_tuple} is too old. '
          f'Minimum required is {self.min_slp_version}.')

      # Phillip doesn't work well on unfrozen stadium
      if (
        self.console.slp_version_tuple >= (3, 19, 0)
        and gamestate.stage is melee.Stage.POKEMON_STADIUM
        and not self.console.is_frozen_ps
      ):
        logging.warning('Playing on unfrozen stadium')

      # Check that we picked the desired characters.
      # Skip in netplay mode: port assignments are dynamic and the
      # controller port may not match the bot's actual in-game port.
      if not self._connect_code:
        for controller, player in self._menuing_controllers:
          gs_player = gamestate.players[controller.port]
          desired_character = player.character
          actual_character = gs_player.character
          if actual_character != desired_character:
            raise WrongCharacterSelected(
              f'Port {controller.port}: expected character '
              f'{desired_character.name}, got {actual_character.name}'
            )

    return gamestate

  def step(self) -> melee.GameState:
    gamestate = self.next_gamestate()

    # The console object keeps track of how long your bot is taking to process frames
    #   And can warn you if it's taking too long
    # if self.console.processingtime * 1000 > 12:
    #     print("WARNING: Last frame took " + str(self.console.processingtime*1000) + "ms to process.")

    menu_frames = 0
    while is_menu_state(gamestate):
      for i, (controller, player) in enumerate(self._menuing_controllers):

        self.menu_helper.menu_helper_simple(
            gamestate, controller,
            stage_selected=self.stage,
            connect_code=self._connect_code,
            autostart=self._autostart and i == 0 and menu_frames > 30,
            swag=False,
            costume=i,
            **player.menuing_kwargs())

      gamestate = self.next_gamestate()
      menu_frames += 1

    return gamestate

  def iter_gamestates(self, skip_menu_frames: bool = True) -> Iterator[melee.GameState]:
    while True:
      gamestate = self.next_gamestate()

      menu_frames = 0
      while is_menu_state(gamestate):
        if not skip_menu_frames:
          yield gamestate

        for i, (controller, player) in enumerate(self._menuing_controllers):

          self.menu_helper.menu_helper_simple(
              gamestate, controller,
              stage_selected=self.stage,
              connect_code=self._connect_code,
              autostart=self._autostart and i == 0 and menu_frames > 180,
              swag=False,
              costume=i,
              **player.menuing_kwargs())

        gamestate = self.next_gamestate()
        menu_frames += 1

      yield gamestate

  @staticmethod
  def _inject_gecko_codes(console: melee.Console, codes: list[GeckoCode]):
    """Inject custom gecko codes into the GALE01r2.ini after libmelee writes it."""
    ini_path = os.path.join(
        console._get_dolphin_home_path(), 'GameSettings', 'GALE01r2.ini')

    with open(ini_path, 'r') as f:
      content = f.read()

    enabled_lines = '\n'.join(code.enabled_line for code in codes)
    code_definitions = '\n'.join(
        f'{code.header}\n{code.code}' for code in codes)

    content = content.replace(
        '[Gecko_Enabled]',
        f'[Gecko_Enabled]\n{enabled_lines}',
        1)
    content = content.rstrip('\n') + '\n\n' + code_definitions + '\n'

    with open(ini_path, 'w') as f:
      f.write(content)

  @staticmethod
  def _inject_netplay_settings(
      console: melee.Console,
      netplay_port: Optional[int] = None,
      lan_ip: Optional[str] = None,
  ):
    """Inject Force Netplay Port / Force LAN IP into Dolphin.ini."""
    ini_path = os.path.join(
        console._get_dolphin_config_path(), 'Dolphin.ini')

    config = configparser.ConfigParser()
    config.optionxform = str  # preserve case for Slippi keys
    config.read(ini_path)

    # Ishiiruka uses [Core] with Slippi* prefixes;
    # mainline uses [Slippi] without prefixes.
    if console.is_mainline:
      section = 'Slippi'
      prefix = ''
    else:
      section = 'Core'
      prefix = 'Slippi'

    if not config.has_section(section):
      config.add_section(section)

    if netplay_port is not None:
      config.set(section, f'{prefix}ForceNetplayPort', 'True')
      config.set(section, f'{prefix}NetplayPort', str(netplay_port))

    if lan_ip is not None:
      config.set(section, f'{prefix}ForceLanIp', 'True')
      config.set(section, f'{prefix}LanIp', lan_ip)

    with open(ini_path, 'w') as f:
      config.write(f)

  def stop(self):
    for controller in self.controllers.values():
      controller.disconnect()
    self.console.stop()

  def __del__(self):
    if hasattr(self, 'controllers'):
      self.stop()

  def multi_step(self, n: int):
    for _ in range(n):
      self.step()

_field = lambda f: dataclasses.field(default_factory=f)

@dataclasses.dataclass
class DolphinConfig:
  """Configure dolphin for evaluation."""
  path: Optional[str] = None  # Path to folder containing the dolphin executable
  iso: Optional[str] = None  # Path to melee 1.02 iso.
  copy_home_directory: bool = False  # Copy the dolphin home directory to a temp location.
  stage: melee.Stage = melee.Stage.RANDOM_STAGE  # Which stage to play on.
  online_delay: int = 0  # Simulate online delay.
  blocking_input: bool = True  # Have game wait for AIs to send inputs.
  console_timeout: Optional[float] = None  # Seconds to wait for console inputs before throwing an error.
  slippi_port: Optional[int] = None  # Local ip port to communicate with dolphin.
  fullscreen: bool = False # Run dolphin in full screen mode
  render: Optional[bool] = None  # Render frames. Only disable if using vladfi1\'s slippi fork.
  save_replays: bool = False  # Save slippi replays to the usual location.
  replay_dir: Optional[str] = None  # Directory to save replays to.
  gfx_backend: str = ''  # Graphics backend to use.
  disable_audio: bool = False  # Disable dolphin audio.
  audio_backend: str = ''  # Audio backend to use.
  headless: bool = True  # Headless configuration: exi + ffw, no graphics or audio.
  emulation_speed: float = 1.0  # Set to 0 for unlimited speed. Mainline only.
  overclock: Optional[float] = None  # CPU overclock multiplier (e.g. 4.0 for 400%). None to disable.
  infinite_time: bool = True  # Infinite time no stocks.
  log_level: int = 3  # WARN; 0 to disable
  log_types: list[str] = dataclasses.field(default_factory=['SLIPPI'].copy)
  dump: DumpConfig = _field(DumpConfig)  # For framedumping.

  # Custom gecko codes
  gecko_codes_file: Optional[str] = None  # Path to file with custom gecko codes in INI format.

  # For online play
  connect_code: Optional[str] = None
  user_json_path: Optional[str] = None

  def to_kwargs(self) -> dict:
    kwargs = dataclasses.asdict(self)
    del kwargs['dump']
    kwargs['dump_config'] = self.dump
    return kwargs

  @classmethod
  def kwargs_from_flags(cls, flags: dict) -> dict:
    kwargs = flags.copy()
    del kwargs['dump']
    kwargs['dump_config'] = DumpConfig(**flags['dump'])
    return kwargs

# TODO: replace usage with the above dataclass
DOLPHIN_FLAGS = dict(
    path=ff.String(None, 'Path to folder containing the dolphin executable.'),
    iso=ff.String(None, 'Path to melee 1.02 iso.'),
    stage=ff.EnumClass(melee.Stage.RANDOM_STAGE, melee.Stage, 'Which stage to play on.'),
    online_delay=ff.Integer(0, 'Simulate online delay.'),
    blocking_input=ff.Boolean(True, 'Have game wait for AIs to send inputs.'),
    slippi_port=ff.Integer(None, 'Local ip port to communicate with dolphin.'),
    fullscreen=ff.Boolean(False, 'Run dolphin in full screen mode.'),
    render=ff.Boolean(None, 'Render frames. Only disable if using vladfi1\'s slippi fork.'),
    save_replays=ff.Boolean(False, 'Save slippi replays to the usual location.'),
    replay_dir=ff.String(None, 'Directory to save replays to.'),
    headless=ff.Boolean(
        False, 'Headless configuration: exi + ffw, no graphics or audio.'),
    emulation_speed=ff.Float(1.0),
    infinite_time=ff.Boolean(False, 'Infinite time no stocks.'),
    log_level=ff.Integer(3, 'Dolphin log level, defaults to WARN.'),
    log_types=ff.StringList(['SLIPPI'], 'Enabled logging categories.'),
    disable_audio=ff.Boolean(False, 'Disable dolphin audio.'),
    gecko_codes_file=ff.String(None, 'Path to file with custom gecko codes in INI format.'),
    netplay_port=ff.Integer(None, 'Force Dolphin to use this UDP port for netplay.'),
    lan_ip=ff.String(None, 'Force Dolphin to advertise this LAN IP for netplay.'),
)
