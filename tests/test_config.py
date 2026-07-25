"""Config store: encryption round-trip, redaction, merge, YAML/env overlays."""

from __future__ import annotations

import tomllib

from qbx.config import (
    REDACTED,
    ConfigStore,
    apply_provider_env_keys,
    cli_overrides_from_args,
    config_patch_is_soft,
    env_overrides,
)


def test_secrets_encrypted_on_disk_and_decrypted_in_memory(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "qbt": {"password": "hunter2"},
        "providers": [{"name": "alldebrid", "api_key": "ad-key-123"}],
    })

    assert store.config.qbt.password == "hunter2"
    assert store.config.providers[0].api_key == "ad-key-123"

    raw = tomllib.loads(store.path.read_text())
    assert raw["qbt"]["password"].startswith("enc:")
    assert "hunter2" not in store.path.read_text()
    assert raw["providers"][0]["api_key"].startswith("enc:")

    reopened = ConfigStore(tmp_path)
    assert reopened.config.qbt.password == "hunter2"
    assert reopened.config.providers[0].api_key == "ad-key-123"


def test_redacted_hides_secrets(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "qbt": {"password": "pw"},
        "server": {"api_token": "tokensecret"},
        "anonymity": {"proxy_url": "socks5://user:pass@127.0.0.1:9050"},
        "providers": [{"name": "alldebrid", "api_key": "k"}],
    })
    red = store.redacted()
    assert red["qbt"]["password"] == REDACTED
    assert red["server"]["api_token"] == REDACTED
    assert red["anonymity"]["proxy_url"] == REDACTED
    assert red["providers"][0]["api_key"] == REDACTED


def test_redacted_placeholder_preserves_existing_secret(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "qbt": {"password": "keepme"},
        "server": {"api_token": "keep-token"},
        "anonymity": {"proxy_url": "socks5://user:pass@127.0.0.1:9050"},
        "providers": [{"name": "alldebrid", "api_key": "adkey"}],
    })
    store.update({
        "qbt": {"url": "http://host:8080", "password": REDACTED},
        "server": {"api_token": REDACTED},
        "anonymity": {"proxy_url": REDACTED},
        "providers": [{"name": "alldebrid", "api_key": REDACTED}],
    })
    assert store.config.qbt.password == "keepme"
    assert store.config.qbt.url == "http://host:8080"
    assert store.config.server.api_token == "keep-token"
    assert store.config.anonymity.proxy_url == "socks5://user:pass@127.0.0.1:9050"
    assert store.config.providers[0].api_key == "adkey"


def test_deep_merge_keeps_untouched_fields(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({"interceptor": {"category_filter": "movies"}})
    store.update({"interceptor": {"enabled": False}})
    assert store.config.interceptor.category_filter == "movies"
    assert store.config.interceptor.enabled is False


def test_provisional_yaml_overrides_defaults(tmp_path):
    (tmp_path / "config.provisional.yaml").write_text(
        "interceptor:\n  stalled_min_minutes: 7\n  delivery_mode: download\n"
    )
    store = ConfigStore(tmp_path)
    assert store.config.interceptor.stalled_min_minutes == 7
    assert store.config.interceptor.delivery_mode == "download"


def test_webui_toml_wins_over_env(tmp_path, monkeypatch):
    """WebUI (config.toml) is the top layer; env cannot override saved values."""
    store = ConfigStore(tmp_path)
    store.update({"interceptor": {"stalled_min_minutes": 30, "stalled_only": True}})
    monkeypatch.setenv("QBX_INTERCEPTOR__STALLED_MIN_MINUTES", "11")
    monkeypatch.setenv("QBX_INTERCEPTOR__STALLED_ONLY", "false")
    reopened = ConfigStore(tmp_path)
    assert reopened.config.interceptor.stalled_min_minutes == 30
    assert reopened.config.interceptor.stalled_only is True


def test_env_applies_when_not_in_toml(tmp_path, monkeypatch):
    """Env fills gaps before first WebUI write of that field.

    First boot with no toml seeds env into toml; subsequent loads use toml.
    Here we delete toml after writing provisional+env into memory via a fresh
    dir where only provisional exists, then env, and assert env wins over provisional.
    """
    (tmp_path / "config.provisional.yaml").write_text(
        "interceptor:\n  stalled_min_minutes: 7\n"
    )
    monkeypatch.setenv("QBX_INTERCEPTOR__STALLED_MIN_MINUTES", "11")
    # No config.toml yet — load seeds toml from provisional+env.
    store = ConfigStore(tmp_path)
    assert store.config.interceptor.stalled_min_minutes == 11
    # After seed, toml has 11; changing env must not override WebUI/toml.
    monkeypatch.setenv("QBX_INTERCEPTOR__STALLED_MIN_MINUTES", "99")
    reopened = ConfigStore(tmp_path)
    assert reopened.config.interceptor.stalled_min_minutes == 11


def test_provider_env_keys_seed_then_toml_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_REALDEBRID_API_KEY", "rd-env")
    monkeypatch.setenv("QBX_ALLDEBRID_API_KEY", "ad-env")
    store = ConfigStore(tmp_path)
    names = {p.name: p.api_key for p in store.config.providers}
    assert names["alldebrid"] == "ad-env"
    assert names["realdebrid"] == "rd-env"

    store.update({
        "providers": [
            {"name": "alldebrid", "api_key": "ad-webui", "enabled": True, "priority": 0},
            {"name": "realdebrid", "api_key": "rd-webui", "enabled": True, "priority": 1},
        ]
    })
    monkeypatch.setenv("QBX_REALDEBRID_API_KEY", "rd-env-2")
    reopened = ConfigStore(tmp_path)
    names = {p.name: p.api_key for p in reopened.config.providers}
    assert names["alldebrid"] == "ad-webui"
    assert names["realdebrid"] == "rd-webui"


def test_provider_env_enabled_and_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_ALLDEBRID_API_KEY", "ad")
    monkeypatch.setenv("QBX_ALLDEBRID_ENABLED", "false")
    monkeypatch.setenv("QBX_ALLDEBRID_PRIORITY", "5")
    store = ConfigStore(tmp_path)
    ad = next(p for p in store.config.providers if p.name == "alldebrid")
    assert ad.api_key == "ad"
    assert ad.enabled is False
    assert ad.priority == 5


def test_cli_overrides_below_toml(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({"anonymity": {"proxy_url": "socks5://webui:1", "enabled": True}})
    cli = cli_overrides_from_args(proxy_url="socks5://cli:9050")
    reopened = ConfigStore(tmp_path, cli_overrides=cli)
    assert reopened.config.anonymity.proxy_url == "socks5://webui:1"


def test_cli_overrides_seed_without_toml(tmp_path):
    cli = cli_overrides_from_args(
        proxy_url="socks5://cli:9050",
        alldebrid_api_key="ad-cli",
        alldebrid_enabled=True,
    )
    store = ConfigStore(tmp_path, cli_overrides=cli)
    assert store.config.anonymity.proxy_url == "socks5://cli:9050"
    assert any(p.name == "alldebrid" and p.api_key == "ad-cli" for p in store.config.providers)


def test_env_overrides_helper_nested():
    patch = env_overrides({
        "QBX_SERVER__PORT": "9000",
        "QBX_QBT__VERIFY_TLS": "false",
        "QBX_ANONYMITY__PROXY_URL": "socks5://127.0.0.1:9050",
        "PATH": "/usr/bin",
    })
    assert patch == {
        "server": {"port": 9000},
        "qbt": {"verify_tls": False},
        "anonymity": {"proxy_url": "socks5://127.0.0.1:9050"},
    }


def test_apply_provider_env_keys_helper():
    data = {"providers": [{"name": "alldebrid", "api_key": "x", "enabled": True, "priority": 0}]}
    out = apply_provider_env_keys(data, {"QBX_REALDEBRID_API_KEY": "y"})
    assert len(out["providers"]) == 2
    assert any(p["name"] == "realdebrid" and p["api_key"] == "y" for p in out["providers"])


def test_defaults_include_webseed_delivery(tmp_path):
    store = ConfigStore(tmp_path)
    assert store.config.interceptor.delivery_mode == "webseed"
    assert store.config.interceptor.stalled_only is True


def test_config_patch_is_soft_classification():
    assert config_patch_is_soft({"desktop": {"notifications": False}})
    assert config_patch_is_soft({"updates": {"check_on_startup": False, "channel": "beta"}})
    assert config_patch_is_soft({"matcher": {"enabled": True, "folders": ["/data"]}})
    assert config_patch_is_soft({"interceptor": {"stalled_min_minutes": 45, "delivery_mode": "webseed"}})
    assert config_patch_is_soft({"content_dupes": {"roots": ["/media"], "min_size_bytes": 2048}})
    # Structural interceptor lifecycle → hard
    assert not config_patch_is_soft({"interceptor": {"enabled": False}})
    assert not config_patch_is_soft({"qbt": {"url": "http://127.0.0.1:9090"}})
    assert not config_patch_is_soft({"providers": []})
    assert not config_patch_is_soft({"anonymity": {"enabled": False}})
    assert not config_patch_is_soft({"server": {"api_token": "x"}})
    # Unknown top-level → hard (safe default)
    assert not config_patch_is_soft({"weird": {"x": 1}})
    assert not config_patch_is_soft({})
