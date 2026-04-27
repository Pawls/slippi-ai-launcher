"""Built-in training presets, extracted from the original tkinter screens.

These mirror the _BUILTIN_TEMPLATES dicts in screens/train_il.py and
screens/train_rl.py so the new Tauri frontend can offer the same templates
without re-importing the tkinter modules (which require ttk at import time).

User-saved presets live in LAUNCHER/train_presets.json next to the config
file. Built-in presets cannot be deleted; user presets with the same name as
a built-in override the built-in for that script.
"""

import json
from pathlib import Path
from typing import Any

from LAUNCHER.config import script_dir

_PRESETS_FILE = "train_presets.json"


BUILTIN_PRESETS: dict[str, dict[str, dict]] = {
    "imitation": {
        "Default (all defaults)": {},
        "Imitation Example": {
            "runtime.max_runtime": 518400,
            "runtime.log_interval": 300,
            "runtime.save_interval": 600,
            "runtime.eval_every_n": 5000,
            "runtime.num_eval_steps": 200,
            "dataset.data_dir": "",
            "dataset.meta_path": "",
            "dataset.allowed_characters": "fox",
            "dataset.allowed_opponents": "all",
            "data.batch_size": 512,
            "data.unroll_length": 80,
            "learner.learning_rate": 1e-4,
            "learner.reward_halflife": 4,
            "network.name": "tx_like",
            "network.tx_like.num_layers": 3,
            "network.tx_like.hidden_size": 512,
            "network.tx_like.ffw_multiplier": 2,
            "policy.train_value_head": False,
            "policy.delay": 18,
            "value_function.train_separate_network": True,
            "value_function.separate_network_config": True,
            "value_function.network.name": "tx_like",
            "value_function.network.tx_like.num_layers": 1,
            "value_function.network.tx_like.hidden_size": 512,
            "value_function.network.tx_like.ffw_multiplier": 2,
            "controller_head.name": "autoregressive",
            "controller_head.autoregressive.component_depth": 2,
            "controller_head.autoregressive.residual_size": 128,
        },
    },
    "q_learning": {
        "Default (all defaults)": {},
    },
    "rl_single": {
        "Default (all defaults)": {},
        "RL Example (from scripts/rl_example.sh)": {
            "runtime.tag": "fox_delay_18",
            "runtime.max_step": 10000,
            "runtime.log_interval": 300,
            "runtime.reset_every_n_steps": 512,
            "runtime.burnin_steps_after_reset": 5,
            "learner.learning_rate": 3e-5,
            "learner.value_cost": 1,
            "learner.reward_halflife": 4,
            "learner.reward.damage_ratio": 0.01,
            "learner.reward.ledge_grab_penalty": 0.02,
            "learner.policy_gradient_weight": 5,
            "learner.kl_teacher_weight": 3e-3,
            "learner.ppo.num_epochs": 2,
            "learner.ppo.num_batches": 16,
            "learner.ppo.beta": 3e-1,
            "learner.ppo.epsilon": 1e-2,
            "learner.ppo.minibatched": False,
            "teacher": "",
            "opponent.type": "SELF",
            "opponent.train": True,
            "actor.rollout_length": 240,
            "actor.num_envs": 96,
            "actor.inner_batch_size": 8,
            "actor.async_envs": True,
            "actor.num_env_steps": 4,
            "actor.gpu_inference": True,
            "agent.name": "Master Player",
            "agent.batch_steps": 4,
            "optimizer_burnin_steps": 128,
            "value_burnin_steps": 128,
        },
        "Single-Agent vs Frozen Opponent": {
            "runtime.max_step": 10000000,
            "runtime.max_runtime": 518400,
            "runtime.log_interval": 300,
            "runtime.save_interval": 600,
            "runtime.reset_every_n_steps": 6144,
            "runtime.burnin_steps_after_reset": 5,
            "dolphin.infinite_time": True,
            "dolphin.headless": True,
            "dolphin.log_level": 3,
            "dolphin.console_timeout": 60,
            "learner.learning_rate": 3e-5,
            "learner.value_cost": 1,
            "learner.reward.damage_ratio": 0.01,
            "learner.reward.ledge_grab_penalty": 0.02,
            "learner.reward.stalling_penalty": 0.1,
            "learner.reward.stalling_threshold": 50.0,
            "learner.reward.voluntary_offstage_death_penalty": 0.6,
            "learner.reward_halflife": 8.0,
            "learner.reward.approaching_factor": 0.003,
            "learner.policy_gradient_weight": 3,
            "learner.kl_teacher_weight": 5e-2,
            "learner.reverse_kl_teacher_weight": 5e-2,
            "learner.ppo.num_epochs": 2,
            "learner.ppo.num_batches": 16,
            "learner.ppo.beta": 3e-1,
            "learner.ppo.epsilon": 1e-2,
            "learner.ppo.max_mean_actor_kl": 1e-4,
            "learner.ppo.minibatched": False,
            "opponent.type": "OTHER",
            "opponent.train": False,
            "actor.rollout_length": 60,
            "actor.num_envs": 120,
            "actor.inner_batch_size": 12,
            "actor.async_envs": True,
            "actor.num_env_steps": 4,
            "actor.gpu_inference": True,
            "agent.batch_steps": 4,
            "optimizer_burnin_steps": 128,
            "value_burnin_steps": 128,
        },
    },
    "rl_train_two": {
        "Default (all defaults)": {},
        "Two-Agent Fresh Start": {
            "runtime.max_step": 10000000,
            "runtime.max_runtime": 518400,
            "runtime.log_interval": 300,
            "runtime.save_interval": 600,
            "runtime.reset_every_n_steps": 6144,
            "runtime.burnin_steps_after_reset": 5,
            "dolphin.infinite_time": True,
            "dolphin.headless": True,
            "dolphin.log_level": 3,
            "dolphin.console_timeout": 60,
            "p1.batch_steps": 4,
            "p2.batch_steps": 4,
            "learner.learning_rate": 3e-5,
            "learner.value_cost": 1,
            "learner.reward.damage_ratio": 0.01,
            "learner.reward_halflife": 8.0,
            "learner.policy_gradient_weight": 3,
            "learner.kl_teacher_weight": 5e-2,
            "learner.reverse_kl_teacher_weight": 5e-2,
            "learner.ppo.num_epochs": 2,
            "learner.ppo.num_batches": 16,
            "learner.ppo.beta": 3e-1,
            "learner.ppo.epsilon": 1e-2,
            "learner.ppo.max_mean_actor_kl": 1e-4,
            "learner.ppo.minibatched": False,
            "learner1.reward.ledge_grab_penalty": 0.02,
            "learner1.reward.stalling_penalty": 0.1,
            "learner1.reward.stalling_threshold": 50.0,
            "learner1.reward.approaching_factor": 0.0,
            "learner1.reward.l_cancel_miss_penalty": 0,
            "learner1.reward.voluntary_offstage_death_penalty": 0.5,
            "learner2.learning_rate": 1e-5,
            "actor.rollout_length": 60,
            "actor.num_envs": 120,
            "actor.inner_batch_size": 12,
            "actor.async_envs": True,
            "actor.num_env_steps": 4,
            "actor.gpu_inference": True,
            "optimizer_burnin_steps": 128,
            "value_burnin_steps": 128,
        },
        "Two-Agent Multi-Character": {
            "runtime.max_step": 10000000,
            "runtime.max_runtime": 518400,
            "runtime.log_interval": 300,
            "runtime.save_interval": 600,
            "runtime.reset_every_n_steps": 6144,
            "runtime.burnin_steps_after_reset": 5,
            "dolphin.infinite_time": True,
            "dolphin.headless": True,
            "dolphin.log_level": 3,
            "dolphin.console_timeout": 60,
            "p1.batch_steps": 4,
            "p2.char": "FOX,FALCO,MARTH,SHEIK,JIGGLYPUFF,CPTFALCON,PEACH,YOSHI,POPO,LUIGI,PIKACHU,SAMUS",
            "p2.batch_steps": 4,
            "learner.learning_rate": 3e-5,
            "learner.value_cost": 1,
            "learner.reward.damage_ratio": 0.01,
            "learner.reward_halflife": 8.0,
            "learner.policy_gradient_weight": 3,
            "learner.kl_teacher_weight": 5e-2,
            "learner.reverse_kl_teacher_weight": 5e-2,
            "learner.ppo.num_epochs": 2,
            "learner.ppo.num_batches": 16,
            "learner.ppo.beta": 3e-1,
            "learner.ppo.epsilon": 1e-2,
            "learner.ppo.max_mean_actor_kl": 1e-4,
            "learner.ppo.minibatched": False,
            "learner1.reward.ledge_grab_penalty": 0.02,
            "learner1.reward.stalling_penalty": 0.1,
            "learner1.reward.stalling_threshold": 50.0,
            "learner1.reward.approaching_factor": 0.0,
            "learner1.reward.l_cancel_miss_penalty": 0,
            "learner1.reward.voluntary_offstage_death_penalty": 0.5,
            "learner2.learning_rate": 1e-5,
            "actor.rollout_length": 60,
            "actor.num_envs": 120,
            "actor.inner_batch_size": 12,
            "actor.async_envs": True,
            "actor.num_env_steps": 4,
            "actor.gpu_inference": True,
            "optimizer_burnin_steps": 128,
            "value_burnin_steps": 128,
        },
    },
}


def _presets_path() -> Path:
    return script_dir() / _PRESETS_FILE


def load_user_presets() -> dict[str, dict[str, dict]]:
    """Read user-saved presets from train_presets.json. Returns {} if missing."""
    p = _presets_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # Sanity: coerce to the expected nested shape.
        out: dict[str, dict[str, dict]] = {}
        for script, presets in data.items():
            if isinstance(presets, dict):
                out[script] = {
                    name: vals for name, vals in presets.items()
                    if isinstance(vals, dict)
                }
        return out
    except (OSError, json.JSONDecodeError):
        return {}


def _save_user_presets(data: dict[str, dict[str, dict]]) -> None:
    p = _presets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")


def is_builtin(script_key: str, name: str) -> bool:
    return name in BUILTIN_PRESETS.get(script_key, {})


def merged_presets() -> dict[str, dict[str, dict[str, Any]]]:
    """Return {script_key: {name: {values, builtin}}} merging builtin + user."""
    user = load_user_presets()
    out: dict[str, dict[str, dict[str, Any]]] = {}
    # Start with built-ins so user entries override on name collision.
    for script_key, presets in BUILTIN_PRESETS.items():
        out[script_key] = {
            name: {"values": vals, "builtin": True}
            for name, vals in presets.items()
        }
    for script_key, presets in user.items():
        if script_key not in out:
            out[script_key] = {}
        for name, vals in presets.items():
            builtin = is_builtin(script_key, name)
            out[script_key][name] = {"values": vals, "builtin": builtin}
    return out


def save_user_preset(
    script_key: str, name: str, values: dict[str, Any]
) -> None:
    """Upsert a user preset. Overwriting a built-in name is allowed (user-side
    override); deleting the user entry will restore the built-in."""
    data = load_user_presets()
    data.setdefault(script_key, {})[name] = values
    _save_user_presets(data)


def delete_user_preset(script_key: str, name: str) -> bool:
    """Remove a user preset. Returns True if removed, False if not present.
    Deleting a built-in is rejected — caller should check is_builtin() first
    if it wants to distinguish the two error cases."""
    data = load_user_presets()
    script = data.get(script_key, {})
    if name not in script:
        return False
    del script[name]
    if not script:
        data.pop(script_key, None)
    _save_user_presets(data)
    return True
