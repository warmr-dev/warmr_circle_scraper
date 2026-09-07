"""HTTP client the local connector uses to talk to the Railway backend.

Auth is a connector token obtained once via pairing (see connector_auth on the
server). The token is stored locally in ~/.circle-leads/connector.json, never
in Git and never alongside any Circle secret.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from pathlib import Path

import requests

CONFIG_PATH = Path.home() / ".circle-leads" / "connector.json"


@dataclass
class ConnectorConfig:
    backend_url: str            # e.g. https://your-app.up.railway.app
    token: str | None = None    # connector API token (from pairing)

    @classmethod
    def load(cls) -> "ConnectorConfig | None":
        if not CONFIG_PATH.exists():
            return None
        data = json.loads(CONFIG_PATH.read_text())
        return cls(backend_url=data["backend_url"], token=data.get("token"))

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(
            {"backend_url": self.backend_url, "token": self.token}, indent=2
        ))
        try:
            CONFIG_PATH.chmod(0o600)  # token is local-only; keep it private
        except OSError:
            pass


class BackendClient:
    def __init__(self, config: ConnectorConfig, *, timeout: int = 30):
        self.cfg = config
        self.timeout = timeout
        self._http = requests.Session()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.cfg.token:
            h["Authorization"] = f"Bearer {self.cfg.token}"
        return h

    def _url(self, path: str) -> str:
        return self.cfg.backend_url.rstrip("/") + path

    def claim(self, code: str) -> str:
        """Exchange a pairing code for a connector token; store it."""
        r = self._http.post(
            self._url("/api/connector/claim"),
            json={"code": code, "agent_info": f"{platform.system()} {platform.release()}"},
            headers={"Content-Type": "application/json"}, timeout=self.timeout,
        )
        r.raise_for_status()
        token = r.json()["token"]
        self.cfg.token = token
        self.cfg.save()
        return token

    def heartbeat(self) -> bool:
        try:
            r = self._http.post(self._url("/api/connector/heartbeat"),
                                headers=self._headers(), timeout=self.timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def worklist(self) -> list[dict]:
        """Ask the backend which communities to scan, highest priority first.

        The dashboard is the control plane: this is how a priority change there
        reaches the connector without touching the command line.
        """
        r = self._http.get(self._url("/api/connector/worklist"),
                           headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("communities") or []

    def report_connection(self, **fields) -> None:
        self._http.post(self._url("/api/connector/connections"),
                        json=fields, headers=self._headers(), timeout=self.timeout).raise_for_status()

    def ingest(self, host: str, records: list[dict]) -> dict:
        r = self._http.post(self._url("/api/connector/ingest"),
                            json={"host": host, "records": records},
                            headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()
