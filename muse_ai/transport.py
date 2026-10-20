from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlencode

import websockets

from .errors import MuseProtocolError, MuseRpcError
from .noise import CipherState, EMPTY_AD, NoiseXXInitiator, make_message1_payload
from .wire import (
    ApplicationRequest,
    ApplicationResponse,
    DecodedServiceFrame,
    Header,
    NoiseReassembler,
    SERVICE_AUTHD,
    SERVICE_DAEMON,
    SERVICE_SENTINEL,
    SERVICE_VAULT,
    decode_service_frame,
    decode_service_response,
    encode_service_frame_request,
    encode_service_request,
    split_noise_payload,
)

SERVICES = {
    "daemon": SERVICE_DAEMON,
    "sentinel": SERVICE_SENTINEL,
    "vault": SERVICE_VAULT,
    "authd": SERVICE_AUTHD,
}


@dataclass(slots=True)
class RpcResponse:
    status: int
    headers: list[Header]
    body: bytes

    def json(self):
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def raise_for_status(self) -> "RpcResponse":
        if self.status >= 400:
            raise MuseRpcError(
                f"Hatch RPC returned HTTP {self.status}: {self.text()[:500]}",
                status=self.status,
                body=self.body,
            )
        return self


class NoiseTransport:
    def __init__(self, tx: CipherState, rx: CipherState) -> None:
        self.tx = tx
        self.rx = rx
        self.next_stream_id = 1
        self.reassembler = NoiseReassembler()

    def encrypt_request(
        self,
        *,
        method: str,
        path: str,
        headers: list[Header],
        body: bytes,
        service: str = "daemon",
    ) -> tuple[int, list[bytes]]:
        stream_id = self.next_stream_id
        self.next_stream_id += 1
        app = ApplicationRequest(method, path, headers, body, True)
        service_frame = encode_service_frame_request(stream_id, app)
        envelope = encode_service_request(SERVICES[service], service_frame)
        encrypted = [
            self.tx.encrypt_with_ad(EMPTY_AD, chunk)
            for chunk in split_noise_payload(envelope)
        ]
        return stream_id, encrypted

    def decrypt_ws_frame(self, encrypted: bytes) -> DecodedServiceFrame | None:
        raw_chunk = self.rx.decrypt_with_ad(EMPTY_AD, encrypted)
        assembled = self.reassembler.feed(raw_chunk)
        if assembled is None:
            return None
        service_payload = decode_service_response(assembled)
        return decode_service_frame(service_payload)


@dataclass
class _StreamState:
    method: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


class HatchConnection:
    def __init__(self, ws, transport: NoiseTransport, *, locale: str = "en-US") -> None:
        self.ws = ws
        self.transport = transport
        self.locale = locale
        self._streams: dict[int, _StreamState] = {}
        self._send_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._fatal: BaseException | None = None
        self._receiver_task = asyncio.create_task(
            self._receiver_loop(), name="muse-hatch-noise-receiver"
        )

    @classmethod
    async def connect(
        cls,
        ws_url: str,
        *,
        locale: str = "en-US",
        timeout: float = 15.0,
    ) -> "HatchConnection":
        ws = await websockets.connect(
            ws_url,
            max_size=None,
            open_timeout=timeout,
            ping_interval=20,
            ping_timeout=20,
        )
        noise = NoiseXXInitiator()
        _client_nonce, payload1 = make_message1_payload()
        await ws.send(noise.write_message1(payload1))
        message2 = await asyncio.wait_for(ws.recv(), timeout)
        if not isinstance(message2, (bytes, bytearray)):
            await ws.close()
            raise MuseProtocolError("expected binary Noise handshake message2")
        # The captured primary VM flow is a standard VM: message2 carries attestation
        # bytes but no owner recovery challenge.  We decrypt the attestation here.
        # Full SNP attestation verification is intentionally a separate hardening step.
        noise.read_message2(bytes(message2))
        await ws.send(noise.write_message3(b""))
        tx, rx = noise.split()
        return cls(ws, NoiseTransport(tx, rx), locale=locale)

    def _headers(
        self,
        *,
        json_body: bool,
        extra: list[Header] | None = None,
    ) -> list[Header]:
        headers: list[Header] = []
        if json_body:
            headers.append(Header("Content-Type", "application/json"))
        headers.extend(
            [
                Header("x-request-id", str(uuid.uuid4())),
                Header("x-app-id", "hatch-web"),
                Header("Accept-Language", self.locale),
            ]
        )
        if extra:
            headers.extend(extra)
        return headers

    async def _receiver_loop(self) -> None:
        try:
            async for message in self.ws:
                if not isinstance(message, (bytes, bytearray)):
                    continue
                frame = self.transport.decrypt_ws_frame(bytes(message))
                if frame is None:
                    continue
                state = self._streams.get(frame.stream_id)
                if state is not None:
                    await state.queue.put(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fatal = exc
        finally:
            self._closed.set()
            for state in list(self._streams.values()):
                await state.queue.put(None)

    async def _open(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        json_body: bool,
        service: str,
        extra_headers: list[Header] | None,
    ) -> tuple[int, _StreamState]:
        if self._closed.is_set():
            raise MuseProtocolError(f"Noise connection is closed: {self._fatal!r}")
        async with self._send_lock:
            stream_id, frames = self.transport.encrypt_request(
                method=method,
                path=path,
                headers=self._headers(json_body=json_body, extra=extra_headers),
                body=body,
                service=service,
            )
            state = _StreamState(method=method)
            self._streams[stream_id] = state
            try:
                for frame in frames:
                    await self.ws.send(frame)
            except BaseException:
                self._streams.pop(stream_id, None)
                raise
        return stream_id, state

    @staticmethod
    def _encode_request(
        method: str, path: str, params: dict | None
    ) -> tuple[str, bytes, bool]:
        if method.upper() == "GET":
            query = urlencode(params or {}, doseq=True)
            if query:
                path = f"{path}{'&' if '?' in path else '?'}{query}"
            return path, b"", False
        body = json.dumps(
            params or {}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return path, body, True

    async def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        *,
        service: str = "daemon",
        extra_headers: list[Header] | None = None,
        timeout: float = 60.0,
    ) -> RpcResponse:
        path, body, json_body = self._encode_request(method, path, params)
        stream_id, state = await self._open(
            method.upper(),
            path,
            body=body,
            json_body=json_body,
            service=service,
            extra_headers=extra_headers,
        )
        status = 0
        headers: list[Header] = []
        chunks = bytearray()
        try:
            async with asyncio.timeout(timeout):
                while True:
                    frame = await state.queue.get()
                    if frame is None:
                        raise MuseProtocolError(
                            f"Noise connection closed during {state.method}: {self._fatal!r}"
                        )
                    if frame.kind == "reset":
                        code, reason = frame.payload
                        raise MuseRpcError(
                            f"Hatch stream reset code={code}: {reason}"
                        )
                    if frame.kind == "response":
                        response: ApplicationResponse = frame.payload
                        status = response.status
                        headers = response.headers or []
                        chunks += response.body
                        if response.end_body:
                            break
                    elif frame.kind == "body_chunk":
                        data, end_body = frame.payload
                        chunks += data
                        if end_body:
                            break
        finally:
            self._streams.pop(stream_id, None)
        return RpcResponse(status=status, headers=headers, body=bytes(chunks))

    async def subscribe(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        *,
        service: str = "daemon",
        extra_headers: list[Header] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[dict]:
        path, body, json_body = self._encode_request(method, path, params)
        stream_id, state = await self._open(
            method.upper(),
            path,
            body=body,
            json_body=json_body,
            service=service,
            extra_headers=extra_headers,
        )
        buffer = bytearray()

        async def next_frame():
            if timeout is None:
                return await state.queue.get()
            return await asyncio.wait_for(state.queue.get(), timeout)

        try:
            while True:
                frame = await next_frame()
                if frame is None:
                    raise MuseProtocolError(
                        f"Noise connection closed during subscription: {self._fatal!r}"
                    )
                if frame.kind == "reset":
                    code, reason = frame.payload
                    raise MuseRpcError(
                        f"Hatch subscription reset code={code}: {reason}"
                    )
                end_body = False
                if frame.kind == "response":
                    response: ApplicationResponse = frame.payload
                    if response.status >= 400:
                        raise MuseRpcError(
                            f"Hatch subscription HTTP {response.status}: "
                            f"{response.body[:500]!r}",
                            status=response.status,
                            body=response.body,
                        )
                    buffer += response.body
                    end_body = response.end_body
                elif frame.kind == "body_chunk":
                    data, end_body = frame.payload
                    buffer += data
                else:
                    continue

                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer[:] = rest
                    if line.strip():
                        yield json.loads(line)

                if end_body:
                    if buffer.strip():
                        yield json.loads(buffer)
                    return
        finally:
            self._streams.pop(stream_id, None)

    async def close(self) -> None:
        if not self._closed.is_set():
            await self.ws.close()
        if not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
