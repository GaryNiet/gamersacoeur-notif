"""Configuration loading.

The configuration lives in a YAML file (see ``config.example.yaml``).
Any ``${VAR}`` in the file is replaced by the environment variable ``VAR``,
so secrets (passwords, client secret) can stay out of the file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
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


@dataclass(frozen=True)
class Streamer:
    """A streamer who connected their Streamlabs account (stored in the database)."""
    id: str
    name: str
    keywords: tuple[str, ...] = ()
    main: bool = False


@dataclass
class MailConfig:
    # "imap"  = any mailbox with IMAP + password (Gmail app password, Infomaniak...)
    # "graph" = Microsoft 365 / Outlook through Microsoft Graph, using an app
    #           registration (client id + secret) with the Mail.Read permission
    provider: str = "imap"
    folder: str = "INBOX"
    poll_seconds: int = 15
    # Only emails matching ALL these filters are considered (each list: any
    # entry, case-insensitive substring; an empty list disables the filter).
    from_contains: list[str] = field(default_factory=list)
    subject_contains: list[str] = field(default_factory=list)
    body_contains: list[str] = field(default_factory=list)
    # On the very first start: False = ignore emails already in the mailbox.
    process_existing: bool = False
    # --- IMAP
    host: str = ""
    username: str = ""
    password: str = ""
    port: int = 993
    use_ssl: bool = True
    # --- Microsoft Graph
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    mailbox: str = ""          # address of the mailbox to read

    def validate(self) -> None:
        required = {"imap": ("host", "username", "password"),
                    "graph": ("tenant_id", "client_id", "client_secret", "mailbox")}
        if self.provider not in required:
            raise ConfigError("mail.provider must be 'imap' or 'graph'")
        missing = [f for f in required[self.provider] if not getattr(self, f)]
        if missing:
            raise ConfigError(f"mail.{', mail.'.join(missing)} required for provider "
                              f"'{self.provider}'")

    @property
    def description(self) -> str:
        if self.provider == "graph":
            return f"{self.mailbox} (Microsoft Graph) / {self.folder}"
        return f"{self.username} @ {self.host} / {self.folder}"


@dataclass
class ParsingConfig:
    # Regexes applied to "subject + newline + body text". The first capture
    # group (or the named group ``value``) is the extracted value. Each can
    # also be a list of regexes: the first one that finds something wins.
    amount: str | list[str] | None = None
    donor: str | list[str] | None = None
    message: str | list[str] | None = None
    # Text in which to look for a streamer keyword. If not set, the whole
    # email (subject + body) is searched.
    streamer: str | list[str] | None = None
    currency: str = "CHF"
    default_donor: str = "Anonyme"
    # When one email can contain several donations: a regex matching the
    # first line of each donation. The email is cut at each match and every
    # part is a separate donation (text before the first match is ignored).
    split_on: str | None = None


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
    # Minimum time between two alerts on the same Streamlabs account. Emails
    # arriving together are queued and played one after the other.
    min_interval_seconds: float = 10


@dataclass
class FtpConfig:
    host: str
    username: str
    password: str
    port: int = 21
    # true = FTPS (encrypted, "explicit TLS"). Only set false if the server
    # doesn't support it: plain FTP sends the password unencrypted.
    tls: bool = True
    directory: str = ""        # remote folder ("" = the login folder)


@dataclass
class ProgressConfig:
    """Upload a progress_bar.json (read by the overlay page) over FTP."""
    ftp: FtpConfig
    enabled: bool = True
    objective: float = 4000
    # Added to the TWINT total ("heros_locaux"), e.g. cash donations. Can be
    # negative to remove test donations.
    offset: float = 0
    # Streamlabs Charity team API; its amount_raised goes in "sl_charity".
    # Empty = no Streamlabs Charity total.
    charity_url: str = ""
    interval_seconds: int = 30
    filename: str = "progress_bar.json"


@dataclass
class Config:
    mail: MailConfig
    parsing: ParsingConfig
    streamlabs: StreamlabsConfig
    secret_key: str
    admin_password: str
    # Streamlabs/Twitch/YouTube names of the account(s) that receive EVERY
    # donation. They become "main" automatically when they connect (can also
    # be toggled on /admin).
    main_accounts: list[str] = field(default_factory=list)
    # true  = anyone who connects starts receiving alerts right away
    # false = new streamers wait until an admin enables them on /admin
    new_streamers_enabled: bool = True
    database: str = "gamersacoeur.sqlite3"
    host: str = "127.0.0.1"
    port: int = 8000
    progress_bar: ProgressConfig | None = None

    def is_main_account(self, names: list[str]) -> bool:
        wanted = {n.strip().casefold() for n in self.main_accounts}
        return any(n.strip().casefold() in wanted for n in names if n)


def _stringify(obj):
    """YAML reads `secret_key: 123456` or `password: 1234` as numbers: turn
    numbers back into text for every field that expects text."""
    for f in fields(obj):
        value = getattr(obj, f.name)
        if str(f.type).startswith("str") and isinstance(value, (int, float)):
            setattr(obj, f.name, str(value))
    return obj


def _build(cls, data: dict, section: str):
    try:
        return _stringify(cls(**data))
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
    for key in ("mail", "streamlabs", "secret_key", "admin_password"):
        if key not in raw:
            raise ConfigError(f"Missing '{key}' in config")
    if "streamers" in raw:
        raise ConfigError("'streamers' is no longer used: streamers sign up themselves with "
                          "the link. Use 'main_accounts' for the main account.")

    sl = _build(StreamlabsConfig, raw["streamlabs"], "streamlabs")
    if sl.mode not in ("donation", "alert"):
        raise ConfigError("streamlabs.mode must be 'donation' or 'alert'")
    main = raw.get("main_accounts") or []
    if isinstance(main, str):
        main = [main]

    top = {k: v for k, v in raw.items()
           if k in ("database", "host", "port", "secret_key", "admin_password",
                    "new_streamers_enabled")}
    mail = _build(MailConfig, raw["mail"], "mail")
    mail.validate()

    progress = None
    if raw.get("progress_bar"):
        pb = dict(raw["progress_bar"])
        if "ftp" not in pb:
            raise ConfigError("progress_bar.ftp (host, username, password) is required")
        pb["ftp"] = _build(FtpConfig, pb["ftp"] or {}, "progress_bar.ftp")
        progress = _build(ProgressConfig, pb, "progress_bar")

    return _stringify(Config(
        mail=mail,
        progress_bar=progress,
        parsing=_build(ParsingConfig, raw.get("parsing") or {}, "parsing"),
        streamlabs=sl,
        main_accounts=[str(m) for m in main],
        **top,
    ))
