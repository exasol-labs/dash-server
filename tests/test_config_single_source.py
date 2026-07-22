"""P6: settings dataclass defaults are the single source, verified against Config.

The consumption defaults live once on ``ConsumptionPolicy``'s fields; ``Config``
parses the same keys from the environment. This pins the two together so a
default changed in one place without the other fails loudly (the plan's
"diff settings-dataclass field defaults against Config under an empty
environment" gate).
"""

from __future__ import annotations

from dash_server.config import Config
from dash_server.consumption.models import ConsumptionPolicy


def _config_consumption_keys() -> dict[str, object]:
    return {
        key: getattr(Config, key)
        for key in vars(Config)
        if key.startswith("DASH_SERVER_CONSUMPTION")
    }


def test_consumption_policy_field_defaults_match_config_under_empty_env():
    # The test environment sets no DASH_SERVER_CONSUMPTION_* overrides, so
    # Config's class attributes are its shipped defaults.
    policy_from_config = ConsumptionPolicy.from_config(_config_consumption_keys())
    assert policy_from_config == ConsumptionPolicy()


def test_consumption_policy_partial_dict_still_uses_field_defaults():
    # An embedder/test passing only a couple of keys must not KeyError: absent
    # keys fall back to the field defaults, present ones win.
    policy = ConsumptionPolicy.from_config({"DASH_SERVER_CONSUMPTION_MAX_ROWS": 7})
    assert policy.max_rows == 7
    assert policy.max_bytes == ConsumptionPolicy().max_bytes
    assert policy.enabled is True
