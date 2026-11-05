"""HTTP client of the BA2 management API: config from env vars, error mapping, no retries of writes."""
import os
from typing import Any, Dict, Optional

import httpx

DEFAULT_TIMEOUT = 60.0


class ApiCallError(Exception):
    """A failed API call, already phrased for the caller (an LLM). Never contains the token."""


class Ba2Client:
    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT,
                 http: Optional[httpx.Client] = None):
        self.base_url = base_url.rstrip("/")
        self._token = token or None
        self.timeout = timeout
        # ``http`` lets tests inject a starlette TestClient (an httpx.Client) bound to the in-process app.
        self._http = http if http is not None else httpx.Client(base_url=self.base_url + "/", timeout=timeout)

    def __repr__(self) -> str:                      # never show the token
        return f"Ba2Client(base_url={self.base_url!r}, token={'set' if self._token else 'unset'})"

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Ba2Client":
        env = os.environ if env is None else env
        if not env.get("BA2_API_URL"):
            raise ApiCallError("BA2_API_URL is not set. Set it to the platform's API root, e.g. "
                               "http://localhost:8081/api/v1")
        timeout = float(env["BA2_API_TIMEOUT"]) if env.get("BA2_API_TIMEOUT") else DEFAULT_TIMEOUT
        return cls(env["BA2_API_URL"], env.get("BA2_API_TOKEN"), timeout)

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "<token>") if self._token else text

    def request(self, method: str, path: str, *, query: Optional[Dict[str, Any]] = None, body: Any = None,
                idempotency_key: Optional[str] = None, write: bool = False) -> Any:
        params = {k: ("true" if v is True else "false" if v is False else v)
                  for k, v in (query or {}).items() if v is not None}
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url = path.lstrip("/")
        try:
            resp = self._http.request(method, url, params=params, json=body, headers=headers)
        except httpx.TimeoutException:
            if write:
                raise ApiCallError(
                    f"The request timed out after {self.timeout:g} s. It was NOT retried and MAY have been executed "
                    "by the platform: check the transaction/order (get_transaction, list_orders, "
                    "get_broker_orders) before trying again.") from None
            raise ApiCallError(f"The request timed out after {self.timeout:g} s (BA2_API_TIMEOUT).") from None
        except httpx.ConnectError:
            raise ApiCallError(f"Cannot connect to {self.base_url}. Check BA2_API_URL and that the platform is "
                               "running. Nothing was sent.") from None
        except httpx.HTTPError as e:
            raise ApiCallError(self._scrub(f"HTTP transport error: {type(e).__name__}")) from None
        if resp.status_code >= 400:
            raise ApiCallError(self._scrub(self._error_text(resp)))
        try:
            return resp.json()
        except ValueError:
            raise ApiCallError(f"The API answered {resp.status_code} with a non-JSON body.") from None

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        try:
            env = resp.json()
        except ValueError:
            env = None
        if isinstance(env, dict) and "error" in env:
            code, message, detail = env["error"], env["message"], env["detail"] if "detail" in env else {}
        else:
            code, message, detail = "http_error", resp.text[:300], {}
        text = f"API error {resp.status_code} [{code}]: {message}"
        if detail:
            text += f" (detail: {detail})"
        if resp.status_code == 401:
            text += (" -- the API token is missing or wrong: set BA2_API_TOKEN to the token from the platform's "
                     "Settings -> Management API -> Generate.")
        elif resp.status_code == 403 and code == "remote_disabled":
            text += (" -- this machine is not local to the platform: turn on 'Allow API access from other "
                     "machines' (api_allow_remote) in Settings -> Management API.")
        elif resp.status_code == 403:
            text += (" -- writes (and reads from other machines) need a token: generate one in Settings -> "
                     "Management API and set BA2_API_TOKEN.")
        return text
