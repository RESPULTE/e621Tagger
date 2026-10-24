"""Bounded HTTP calls. Never include credential-bearing URLs in errors or logs."""

import base64
import json
import ssl
import urllib.error
import urllib.request


class ApiError(RuntimeError):
    def __init__(self, code, payload=None):
        super().__init__(f"HTTP operation failed ({code})")
        self.code = code
        self.payload = payload or {}


def request(url, payload=None, *, auth=None, headers=None, cafile=None, timeout=5, method=None):
    values = dict(headers or {})
    if auth:
        encoded = base64.b64encode(":".join(auth).encode()).decode()
        values["Authorization"] = "Basic " + encoded
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        values["Content-Type"] = "application/json"
    operation = urllib.request.Request(url, data=body, headers=values, method=method)
    try:
        with urllib.request.urlopen(operation, timeout=timeout,
                                    context=ssl.create_default_context(cafile=cafile)) as response:
            data = response.read(4 * 1024**2 + 1)
            if len(data) > 4 * 1024**2:
                raise ApiError("response_too_large")
            if not data:
                return {}
            try:
                return json.loads(data)
            except ValueError:
                return {"text": data.decode(errors="replace")[:1000]}
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read(16384))
        except ValueError:
            detail = {}
        raise ApiError(error.code, detail) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiError("transport") from None


class OpenObserve:
    def __init__(self, config, secrets):
        self.url = config["openobserve_url"].rstrip("/")
        self.organization = config.get("organization", "default")
        self.auth = (secrets["query_user"], secrets["query_password"])
        self.cafile = config.get("ca_file")

    def search(self, sql, start, end, size=500, offset=0):
        body = {"query": {"sql": sql, "start_time": int(start * 1e6),
                          "end_time": int(end * 1e6), "from": offset, "size": size}}
        data = request(f"{self.url}/api/{self.organization}/_search", body,
                       auth=self.auth, cafile=self.cafile, timeout=8)
        if data.get("is_partial") or data.get("function_error"):
            raise ApiError("partial_search")
        return data.get("hits", [])


def deliver(incidents, secrets, *, now):
    token, chat_id = secrets.get("telegram_token"), secrets.get("telegram_chat_id")
    if not token or not chat_id:
        return {"configured": False, "delivered": 0}
    rows = incidents.db.execute(
        "SELECT id,body,attempts FROM outbox "
        "WHERE due<=? AND kind IN ('error','completion') ORDER BY id LIMIT 5", (now,)
    ).fetchall()
    sent = 0
    for row_id, body, attempts in rows:
        try:
            payload = {"chat_id": chat_id, "text": body,
                       "link_preview_options": {"is_disabled": True}}
            result = request(f"https://api.telegram.org/bot{token}/sendMessage",
                             payload, timeout=5)
            if not result.get("ok"):
                raise ApiError(result.get("error_code", "telegram_rejected"), result)
            incidents.db.execute("DELETE FROM outbox WHERE id=?", (row_id,))
            sent += 1
        except ApiError as error:
            delay = max(30, min(900, 30 * 2 ** min(attempts, 5)))
            delay = max(delay, error.payload.get("parameters", {}).get("retry_after", 0))
            if error.code in {400, 401, 403}:
                delay = 3600
            incidents.db.execute("UPDATE outbox SET due=?,attempts=attempts+1 WHERE id=?",
                                 (now + delay, row_id))
        incidents.db.commit()
    return {"configured": True, "delivered": sent}
