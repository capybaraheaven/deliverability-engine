"""Configuration loading: policy from config.yaml, secrets from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without adding a dependency.

    Values already present in the environment win, so an exported variable or a
    CI secret overrides the file.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Secrets:
    smartlead_api_key: str
    zapmail_api_key: str
    zapmail_workspace_key: str | None
    database_url: str | None
    sqlite_path: str
    slack_webhook_url: str | None


class Config:
    """Policy plus secrets. Attribute access mirrors the config.yaml sections."""

    def __init__(self, policy: dict, secrets: Secrets, path: Path):
        self.path = path
        self._policy = policy
        self.secrets = secrets
        self.smartlead = policy["smartlead"]
        self.zapmail = policy["zapmail"]
        self.mailbox = policy["mailbox"]
        self.domain = policy["domain"]
        self.campaign = policy["campaign"]
        self.rotation = policy["rotation"]

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        cfg_path = Path(path) if path else ROOT / "config.yaml"
        policy = yaml.safe_load(cfg_path.read_text())
        _load_dotenv(ROOT / ".env")
        secrets = Secrets(
            smartlead_api_key=os.environ.get("SMARTLEAD_API_KEY", ""),
            zapmail_api_key=os.environ.get("ZAPMAIL_API_KEY", ""),
            zapmail_workspace_key=os.environ.get("ZAPMAIL_WORKSPACE_KEY") or None,
            database_url=os.environ.get("DATABASE_URL") or None,
            sqlite_path=os.environ.get("SQLITE_PATH", "state.db"),
            slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL") or None,
        )
        return cls(policy, secrets, cfg_path)

    def require(self, *names: str) -> None:
        """Fail before any network call rather than halfway through a run."""
        missing = [n for n in names if not getattr(self.secrets, n)]
        if missing:
            raise SystemExit(
                "Missing required environment variables: "
                + ", ".join(n.upper() for n in missing)
                + f"\nCopy .env.example to .env in {ROOT} and fill them in."
            )
