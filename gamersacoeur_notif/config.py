"""Configuration loading.

The configuration lives in a YAML file (see ``config.example.yaml``).
Any ``${VAR}`` in the file is replaced by the environment variable ``VAR``,
so secrets (passwords, client secret) can stay out of the file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value):
    if isinstance(value, str):
        def repl(m):
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"Environment variable {name} is not set")
            return os.environ[name]
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class ConfigError(Exception):
    pass


@dataclass
class Streamer:
    slug: str
    name: str
    keywords: list[str] = field(default_factory=list)
    main: bool = False


@dataclass
class MailConfig:
    host: str
    username: str
    password: str
    port: int = 993
    folder: str = "INBOX"
    use_ssl: bool = True
    poll_seconds: int = 15
    # Only emails matching these (case-insensitive substring) are considered.
    from_contains: list[str] = field(default_factory=list)
    subject_contains: list[str] = field(default_factory=list)
    # On the very first start: False = ignore emails already in the mailbox.
    process_existing: bool = False


@dataclass
class ParsingConfig:
    # Regexes applied to "subject + newline + body text". The first capture
    # group (or the named group ``value``) is the extracted value.
    amount: str | None = None
    donor: str | None = None
    message: str | None = None
    # Text in which to look for a streamer keyword. If not set, the whole
    # email (subject + body) is searched.
    streamer: str | None = None
    currency: str = "CHF"
    default_donor: str = "Anonyme"


@dataclass
class StreamlabsConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    # "donation": creates a Streamlabs donation (alert box donation alert,
    #             appears in recent events / donation goals).
    # "alert":    only triggers an alert box alert, nothing is recorded.
    mode: str = "donation"
    alert_message: str = "*{donor}* a donné *{amount} {currency}* via TWINT !"
    alert_user_message: str = "{message}"
    # When true nothing is sent to Streamlabs; deliveries are only logged.
    dry_run: bool = False


@dataclass
class Config:
    mail: MailConfig
    parsing: ParsingConfig
    streamlabs: StreamlabsConfig
    streamers: list[Streamer]
    secret_key: str
    admin_password: str
    database: str = "gamersacoeur.sqlite3"
    host: str = "127.0.0.1"
    port: int = 8000

    def streamer(self, slug: str) -> Streamer | None:
        return next((s for s in self.streamers if s.slug == slug), None)

    @property
    def main_streamers(self) -> list[Streamer]:
        return [s for s in self.streamers if s.main]


def _build(cls, data: dict, section: str):
    try:
        return cls(**data)
    except TypeError as exc:
        raise ConfigError(f"Invalid '{section}' section: {exc}") from exc


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = _expand_env(raw)
    return config_from_dict(raw)


def config_from_dict(raw: dict) -> Config:
    for key in ("mail", "streamlabs", "streamers", "secret_key", "admin_password"):
        if key not in raw:
            raise ConfigError(f"Missing '{key}' in config")

    streamers = []
    seen = set()
    for i, s in enumerate(raw["streamers"]):
        st = _build(Streamer, s, f"streamers[{i}]")
        if not re.fullmatch(r"[a-z0-9_-]+", st.slug):
            raise ConfigError(f"Streamer slug '{st.slug}' must be lowercase letters/digits/-/_")
        if st.slug in seen:
            raise ConfigError(f"Duplicate streamer slug '{st.slug}'")
        seen.add(st.slug)
        streamers.append(st)
    if not any(s.main for s in streamers):
        raise ConfigError("At least one streamer must have 'main: true'")

    sl = _build(StreamlabsConfig, raw["streamlabs"], "streamlabs")
    if sl.mode not in ("donation", "alert"):
        raise ConfigError("streamlabs.mode must be 'donation' or 'alert'")

    top = {k: v for k, v in raw.items()
           if k in ("database", "host", "port", "secret_key", "admin_password")}
    return Config(
        mail=_build(MailConfig, raw["mail"], "mail"),
        parsing=_build(ParsingConfig, raw.get("parsing") or {}, "parsing"),
        streamlabs=sl,
        streamers=streamers,
        **top,
    )
