"""Alibaba Cloud NLS SpeechTranscriber (realtime) over WebSocket.

Protocol: https://help.aliyun.com/zh/isi/developer-reference/websocket
Token: CreateToken via AccessKey (official POP API 2019-02-28) or
``ALIYUN_NLS_TOKEN``. Never log token / AccessKey / AppKey values.

Do not send Deepgram ``keyterm`` here. Aliyun hotwords use a separate
``vocabulary_id`` configured on the NLS project — not ``terms_zh.json``.
``terms_zh.json`` still feeds ``transcript_fix`` after finals.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from provider import ASR_ALIYUN, AsrConfigError, AsrEvent

DEFAULT_NLS_GATEWAY = "wss://nls-gateway-cn-shanghai.aliyuncs.com/ws/v1"
CREATE_TOKEN_HOST = "nls-meta.cn-shanghai.aliyuncs.com"
CREATE_TOKEN_VERSION = "2019-02-28"
SAMPLE_RATE = 16_000
# Refresh a cached CreateToken this many seconds before ExpireTime.
_TOKEN_SKEW_S = 60.0

_token_lock = threading.Lock()
_token_cache: tuple[str, float] | None = None  # (token, expire_unix)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def aliyun_nls_appkey() -> str:
    return _env("ALIYUN_NLS_APPKEY")


def require_aliyun_nls_config() -> None:
    """Fail loud before connect. Does not print secret values."""
    if not aliyun_nls_appkey():
        raise AsrConfigError(
            "Aliyun NLS requires ALIYUN_NLS_APPKEY "
            "(and ALIYUN_NLS_TOKEN or ALIYUN_ACCESS_KEY_ID + "
            "ALIYUN_ACCESS_KEY_SECRET). Put them in local .env; never commit."
        )
    if _env("ALIYUN_NLS_TOKEN"):
        return
    if _env("ALIYUN_ACCESS_KEY_ID") and _env("ALIYUN_ACCESS_KEY_SECRET"):
        return
    raise AsrConfigError(
        "Aliyun NLS requires ALIYUN_NLS_TOKEN or both "
        "ALIYUN_ACCESS_KEY_ID and ALIYUN_ACCESS_KEY_SECRET. "
        "Put them in local .env; never commit."
    )


def percent_encode(raw: str) -> str:
    """Aliyun POP percent-encode (RFC 3986 subset used by CreateToken)."""
    return quote(str(raw), safe="-_.~").replace("+", "%20").replace("*", "%2A")


def canonical_query(params: dict[str, str]) -> str:
    items = sorted((k, v) for k, v in params.items() if k != "Signature")
    return "&".join(f"{percent_encode(k)}={percent_encode(v)}" for k, v in items)


def sign_pop_rpc(
    method: str, params: dict[str, str], access_key_secret: str
) -> str:
    """HMAC-SHA1 Signature for Aliyun RPC (CreateToken)."""
    string_to_sign = (
        f"{method.upper()}&{percent_encode('/')}&"
        f"{percent_encode(canonical_query(params))}"
    )
    key = (access_key_secret + "&").encode("utf-8")
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def build_create_token_params(
    access_key_id: str,
    *,
    nonce: str,
    timestamp: str,
) -> dict[str, str]:
    return {
        "AccessKeyId": access_key_id,
        "Action": "CreateToken",
        "Format": "JSON",
        "RegionId": "cn-shanghai",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": nonce,
        "SignatureVersion": "1.0",
        "Timestamp": timestamp,
        "Version": CREATE_TOKEN_VERSION,
    }


def _create_token_via_sdk(ak_id: str, ak_secret: str) -> tuple[str, float]:
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest

    client = AcsClient(ak_id, ak_secret, "cn-shanghai")
    request = CommonRequest()
    request.set_method("POST")
    request.set_protocol_type("https")
    request.set_domain(CREATE_TOKEN_HOST)
    request.set_version(CREATE_TOKEN_VERSION)
    request.set_action_name("CreateToken")
    raw = client.do_action_with_exception(request)
    data = json.loads(raw)
    token = str((data.get("Token") or {}).get("Id") or "")
    expire = float((data.get("Token") or {}).get("ExpireTime") or 0)
    if not token:
        raise AsrConfigError("Aliyun CreateToken returned an empty Token.Id")
    return token, expire


def _create_token_via_rpc(ak_id: str, ak_secret: str) -> tuple[str, float]:
    nonce = uuid.uuid4().hex
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    params = build_create_token_params(ak_id, nonce=nonce, timestamp=timestamp)
    params["Signature"] = sign_pop_rpc("POST", params, ak_secret)
    body = urlencode(params).encode("utf-8")
    req = Request(
        f"https://{CREATE_TOKEN_HOST}/",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("Code") or payload.get("Message") or "")
        except Exception:
            detail = str(exc)
        raise AsrConfigError(f"Aliyun CreateToken HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise AsrConfigError(f"Aliyun CreateToken network error: {exc.reason}") from exc
    if not isinstance(data, dict):
        raise AsrConfigError("Aliyun CreateToken returned non-JSON object")
    if data.get("Code") and str(data.get("Code")) not in {"200", "OK", ""}:
        raise AsrConfigError(
            f"Aliyun CreateToken failed: {data.get('Code')} {data.get('Message')}"
        )
    token_obj = data.get("Token") or {}
    token = str(token_obj.get("Id") or "")
    expire = float(token_obj.get("ExpireTime") or 0)
    if not token:
        raise AsrConfigError("Aliyun CreateToken returned an empty Token.Id")
    return token, expire


def create_nls_token(ak_id: str, ak_secret: str) -> tuple[str, float]:
    """Return (token, expire_unix_s). Prefer official SDK when installed."""
    try:
        return _create_token_via_sdk(ak_id, ak_secret)
    except ImportError:
        return _create_token_via_rpc(ak_id, ak_secret)


def resolve_nls_token(*, force_refresh: bool = False) -> str:
    """``ALIYUN_NLS_TOKEN`` or cached CreateToken. Never logs the value."""
    preset = _env("ALIYUN_NLS_TOKEN")
    if preset:
        return preset
    ak_id = _env("ALIYUN_ACCESS_KEY_ID")
    ak_secret = _env("ALIYUN_ACCESS_KEY_SECRET")
    if not ak_id or not ak_secret:
        raise AsrConfigError(
            "Aliyun NLS: set ALIYUN_NLS_TOKEN or AccessKey ID/Secret"
        )
    global _token_cache
    now = time.time()
    with _token_lock:
        if (
            not force_refresh
            and _token_cache is not None
            and _token_cache[1] - _TOKEN_SKEW_S > now
        ):
            return _token_cache[0]
        token, expire = create_nls_token(ak_id, ak_secret)
        _token_cache = (token, expire if expire > 0 else now + 3600)
        return token


def nls_gateway_url(token: str) -> str:
    base = _env("ALIYUN_NLS_URL") or DEFAULT_NLS_GATEWAY
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}token={token}"


def _hex32() -> str:
    return uuid.uuid4().hex


def start_transcription_message(appkey: str, task_id: str) -> dict[str, Any]:
    return {
        "header": {
            "appkey": appkey,
            "message_id": _hex32(),
            "task_id": task_id,
            "namespace": "SpeechTranscriber",
            "name": "StartTranscription",
        },
        "payload": {
            "format": "pcm",
            "sample_rate": SAMPLE_RATE,
            "enable_intermediate_result": True,
            "enable_punctuation_prediction": True,
            "enable_inverse_text_normalization": True,
        },
    }


def stop_transcription_message(appkey: str, task_id: str) -> dict[str, Any]:
    return {
        "header": {
            "appkey": appkey,
            "message_id": _hex32(),
            "task_id": task_id,
            "namespace": "SpeechTranscriber",
            "name": "StopTranscription",
        }
    }


def parse_aliyun_nls_event(
    message: object, *, ts: float | None = None
) -> AsrEvent | None:
    """Map NLS JSON to ``AsrEvent``. None for control / empty / unknown."""
    obj: dict[str, Any]
    if isinstance(message, (bytes, bytearray)):
        try:
            obj = json.loads(bytes(message).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    elif isinstance(message, str):
        try:
            obj = json.loads(message)
        except json.JSONDecodeError:
            return None
    elif isinstance(message, dict):
        obj = message
    else:
        return None
    header = obj.get("header") if isinstance(obj.get("header"), dict) else {}
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    name = str(header.get("name") or "")
    status = header.get("status")
    if status not in (None, 20000000, "20000000") and name == "TaskFailed":
        return None
    text = str(payload.get("result") or "").strip()
    now = time.time() if ts is None else ts
    begin_ms = _as_float(payload.get("begin_time"), 0.0)
    end_ms = _as_float(payload.get("time"), 0.0)
    start_s = begin_ms / 1000.0
    duration_s = max(0.0, (end_ms - begin_ms) / 1000.0) if end_ms else 0.0
    conf = _as_float(payload.get("confidence"), 0.0)
    if name == "SentenceEnd":
        if not text:
            return None
        return AsrEvent(
            text=text,
            is_final=True,
            speech_final=True,
            confidence=conf,
            ts=now,
            start_s=start_s,
            duration_s=duration_s,
            provider=ASR_ALIYUN,
        )
    if name in {"TranscriptionResultChanged", "ResultChanged"}:
        if not text:
            return None
        return AsrEvent(
            text=text,
            is_final=False,
            speech_final=False,
            confidence=conf,
            ts=now,
            start_s=start_s,
            duration_s=duration_s or (end_ms / 1000.0),
            provider=ASR_ALIYUN,
        )
    return None


def _as_float(raw: object, default: float) -> float:
    try:
        if raw is None or raw == "":
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def _nls_error_text(obj: dict[str, Any]) -> str:
    header = obj.get("header") if isinstance(obj.get("header"), dict) else {}
    status = header.get("status")
    text = header.get("status_text") or header.get("status_message") or ""
    return f"NLS {status}: {text}".strip()


class AliyunNlsSession:
    """SpeechTranscriber WS: send 16 kHz mono PCM, emit ``AsrEvent``."""

    provider: str = ASR_ALIYUN

    def __init__(
        self,
        *,
        on_event: Callable[[AsrEvent], None],
        on_error: Callable[[object], None] | None = None,
        handshake_timeout_s: float = 60.0,
        appkey: str | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_error = on_error
        self._handshake_timeout_s = handshake_timeout_s
        self._appkey = (appkey or "").strip()
        self._halt = threading.Event()
        self._audio_q: queue.Queue[bytes | None] = queue.Queue()
        self._ws: Any = None
        self._task_id = _hex32()
        self._recv_thread: threading.Thread | None = None
        self._send_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    def start(self) -> None:
        require_aliyun_nls_config()
        appkey = self._appkey or aliyun_nls_appkey()
        if not appkey:
            raise AsrConfigError("ALIYUN_NLS_APPKEY is empty")
        self._appkey = appkey
        token = resolve_nls_token()
        url = nls_gateway_url(token)
        self._recv_thread = threading.Thread(
            target=self._run, args=(url,), name="aliyun-nls-recv", daemon=True
        )
        self._recv_thread.start()
        if not self._ready.wait(self._handshake_timeout_s):
            self._halt.set()
            raise TimeoutError(
                f"Aliyun NLS handshake timed out after "
                f"{self._handshake_timeout_s:.0f}s (no TranscriptionStarted)"
            )
        if self._start_error is not None:
            raise self._start_error

    def send_pcm(self, chunk: bytes) -> None:
        if self._halt.is_set() or not chunk:
            return
        self._audio_q.put(chunk)

    def close(self) -> None:
        self._halt.set()
        try:
            self._audio_q.put(None)
        except Exception:
            pass
        ws = self._ws
        if ws is not None and self._appkey:
            try:
                ws.send(json.dumps(stop_transcription_message(self._appkey, self._task_id)))
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
        if self._send_thread is not None:
            self._send_thread.join(timeout=2.0)
        if self._recv_thread is not None:
            self._recv_thread.join(timeout=2.0)
        self._ws = None

    def _fail(self, exc: BaseException) -> None:
        self._start_error = exc
        if self._on_error is not None:
            try:
                self._on_error(exc)
            except Exception:
                pass
        self._ready.set()
        self._halt.set()

    def _run(self, url: str) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            self._fail(
                AsrConfigError(
                    f"websockets sync client unavailable ({exc}); "
                    "need websockets>=13"
                )
            )
            return
        try:
            with connect(
                url,
                open_timeout=self._handshake_timeout_s,
                close_timeout=2,
                max_size=None,
            ) as ws:
                self._ws = ws
                ws.send(
                    json.dumps(start_transcription_message(self._appkey, self._task_id))
                )
                deadline = time.monotonic() + self._handshake_timeout_s
                while time.monotonic() < deadline and not self._halt.is_set():
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    if not self._handle_control(raw, waiting_start=True):
                        continue
                    self._ready.set()
                    break
                else:
                    if not self._ready.is_set():
                        self._fail(
                            TimeoutError(
                                "Aliyun NLS: no TranscriptionStarted before timeout"
                            )
                        )
                        return
                self._send_thread = threading.Thread(
                    target=self._sender, name="aliyun-nls-send", daemon=True
                )
                self._send_thread.start()
                while not self._halt.is_set():
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    except Exception as exc:
                        if not self._halt.is_set():
                            self._fail(exc)
                        break
                    self._handle_control(raw, waiting_start=False)
        except BaseException as exc:
            if not self._ready.is_set():
                self._fail(exc)
            elif self._on_error is not None and not self._halt.is_set():
                self._on_error(exc)
        finally:
            self._ws = None
            self._ready.set()

    def _sender(self) -> None:
        while True:
            chunk = self._audio_q.get()
            if chunk is None:
                break
            if self._halt.is_set():
                continue
            ws = self._ws
            if ws is None:
                break
            try:
                ws.send(chunk)
            except Exception as exc:
                self._fail(exc)
                break

    def _handle_control(self, raw: object, *, waiting_start: bool) -> bool:
        if isinstance(raw, (bytes, bytearray)):
            return False
        try:
            obj = json.loads(str(raw))
        except json.JSONDecodeError:
            return False
        if not isinstance(obj, dict):
            return False
        header = obj.get("header") if isinstance(obj.get("header"), dict) else {}
        name = str(header.get("name") or "")
        status = header.get("status")
        if name == "TaskFailed" or (
            status not in (None, 20000000, "20000000") and name
        ):
            self._fail(AsrConfigError(_nls_error_text(obj)))
            return False
        if waiting_start and name == "TranscriptionStarted":
            return True
        event = parse_aliyun_nls_event(obj)
        if event is not None:
            self._on_event(event)
        return False
