#!/usr/bin/env python3

# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import asyncio
import logging
from collections import defaultdict
from typing import Dict, Optional, Any

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.h3.connection import H3_ALPN, H3Connection, Setting
from aioquic.h3.events import H3Event, HeadersReceived, WebTransportStreamDataReceived, DatagramReceived, DataReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import stream_is_unidirectional
from aioquic.quic.events import ProtocolNegotiated, StreamReset, QuicEvent
from aioquic.quic.logger import QuicLogger

from pprint import pprint
import json

BIND_ADDRESS = '0.0.0.0'
BIND_PORT = 4433

logger = logging.getLogger(__name__)
quic_logger = QuicLogger()
logging.basicConfig(level=logging.INFO)


# https://datatracker.ietf.org/doc/html/draft-ietf-masque-h3-datagram-05#section-9.1
H3_DATAGRAM_05 = 0xffd277
# https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-h3-websockets-00#section-5
ENABLE_CONNECT_PROTOCOL = 0x08

class H3ConnectionWithDatagram(H3Connection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    # Overrides H3Connection._validate_settings() to enable HTTP Datagram
    def _validate_settings(self, settings: Dict[int, int]) -> None:
        settings[Setting.H3_DATAGRAM] = 1
        return super()._validate_settings(settings)

    # Overrides H3Connection._get_local_settings() to enable HTTP Datagram and
    # extended CONNECT methods.
    def _get_local_settings(self) -> Dict[int, int]:
        settings = super()._get_local_settings()
        settings[H3_DATAGRAM_05] = 1
        settings[ENABLE_CONNECT_PROTOCOL] = 1
        return settings

class AudioEchoStream:

    def __init__(self, session_id, http: H3ConnectionWithDatagram) -> None:
        self._session_id = session_id
        self._http = http
        self._echo_stream_id = defaultdict(int) 

    def h3_event_received(self, event: H3Event) -> None:
        if isinstance(event, WebTransportStreamDataReceived):
            if event.stream_id not in self._echo_stream_id:
                self._echo_stream_id[event.stream_id] = self._http.create_webtransport_stream(
                    self._session_id, is_unidirectional=True)

            self._http._quic.send_stream_data(
                self._echo_stream_id[event.stream_id], event.data, end_stream=event.stream_ended)

            if event.stream_ended:
                self.stream_closed(event.stream_id)

    def stream_closed(self, stream_id) -> None:
        try:
            del self._echo_stream_id[stream_id]
        except KeyError:
            pass

    def session_closed(self) -> None:
        # 音声送信がストップされた
        # 特に何もする必要はない
        return

class VideoEchoStream:

    def __init__(self, session_id, http: H3ConnectionWithDatagram) -> None:
        self._session_id = session_id
        self._http = http
        self._echo_stream_id = defaultdict(int) 

    def h3_event_received(self, event: H3Event) -> None:
        if isinstance(event, WebTransportStreamDataReceived):
            if event.stream_id not in self._echo_stream_id:
                self._echo_stream_id[event.stream_id] = self._http.create_webtransport_stream(
                    self._session_id, is_unidirectional=True)

            self._http._quic.send_stream_data(
                self._echo_stream_id[event.stream_id], event.data, end_stream=event.stream_ended)

            if event.stream_ended:
                self.stream_closed(event.stream_id)

    def stream_closed(self, stream_id) -> None:
        try:
            del self._echo_stream_id[stream_id]
        except KeyError:
            pass

    def session_closed(self) -> None:
        # ビデオ送信がストップされた
        # 特に何もする必要はない
        return

class AudioEchoDatagram:

    def __init__(self, session_id, http: H3ConnectionWithDatagram, protocol: QuicConnectionProtocol) -> None:
        self._session_id = session_id
        self._http = http
        self._protocol = protocol

    def h3_event_received(self, event: H3Event) -> None:
        if isinstance(event, DatagramReceived):
            # やってきたデータをそのまま流す
            self._http.send_datagram(self._session_id, event.data)
            self._protocol.transmit()

    def stream_closed(self, stream_id) -> None:
        try:
            del self._echo_stream_id[stream_id]
        except KeyError:
            pass

    def session_closed(self) -> None:
        # 音声送信がストップされた
        # 特に何もする必要はない
        return

class VideoEchoDatagram:

    def __init__(self, session_id, http: H3ConnectionWithDatagram, protocol: QuicConnectionProtocol) -> None:
        self._session_id = session_id
        self._http = http
        self._protocol = protocol

    def h3_event_received(self, event: H3Event) -> None:
        if isinstance(event, DatagramReceived):
            # やってきたデータをそのまま流す
            self._http.send_datagram(self._session_id, event.data)
            self._protocol.transmit()

    def stream_closed(self, stream_id) -> None:
        try:
            del self._echo_stream_id[stream_id]
        except KeyError:
            pass

    def session_closed(self) -> None:
        # ビデオ送信がストップされた
        # 特に何もする必要はない
        return

class WebTransportProtocol(QuicConnectionProtocol):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._http: Optional[H3ConnectionWithDatagram] = None
        self._handler = None # パスに応じて ChatHandler, VideoReceiver, VideoSubscriber を使い分ける

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ProtocolNegotiated):
            self._http = H3ConnectionWithDatagram(
                self._quic, enable_webtransport=True)
        elif isinstance(event, StreamReset) and self._handler is not None:
            # Streams in QUIC can be closed in two ways: normal (FIN) and
            # abnormal (resets).  FIN is handled by the handler; the code
            # below handles the resets.
            self._handler.stream_closed(event.stream_id)

        if self._http is not None:
            for h3_event in self._http.handle_event(event):
                self._h3_event_received(h3_event)

    def _h3_event_received(self, event: H3Event) -> None:
        # pprint(event)
        if isinstance(event, HeadersReceived):
            headers = {}
            for header, value in event.headers:
                headers[header] = value
            if (headers.get(b":method") == b"CONNECT" and
                    headers.get(b":protocol") == b"webtransport"):
                self._handshake_webtransport(event.stream_id, headers)
            else:
                self._send_response(event.stream_id, 400, end_stream=True)
        elif isinstance(event, DataReceived):
            # CLOSE_WEBTRANSPORT_SESSION 0x2843 なら退室処理をする なぜか送られてくるデータは0x6843になっている??
            if (hasattr(event, 'data') and len(event.data) >= 3 and event.stream_ended and
                    event.data[0] == 0x68 and event.data[1] == 0x43):
                print("session closed!!!!!!")
                self._handler.session_closed()

        if self._handler:
            self._handler.h3_event_received(event)

    def _handshake_webtransport(self,
                                stream_id: int,
                                request_headers: Dict[bytes, bytes]) -> None:
        authority = request_headers.get(b":authority")
        path = request_headers.get(b":path")
        if authority is None or path is None:
            # `:authority` and `:path` must be provided.
            self._send_response(stream_id, 400, end_stream=True)
            return
        elif path == b"/audio/echo/stream":
            assert(self._handler is None)
            self._handler = AudioEchoStream(stream_id, self._http)
            self._send_response(stream_id, 200)
        elif path == b"/video/echo/stream":
            assert(self._handler is None)
            self._handler = VideoEchoStream(stream_id, self._http)
            self._send_response(stream_id, 200)
        elif path == b"/audio/echo/datagram":
            assert(self._handler is None)
            self._handler = AudioEchoDatagram(stream_id, self._http, self)
            self._send_response(stream_id, 200)
        elif path == b"/video/echo/datagram":
            assert(self._handler is None)
            self._handler = VideoEchoDatagram(stream_id, self._http, self)
            self._send_response(stream_id, 200)
        else:
            self._send_response(stream_id, 404, end_stream=True)

    def _send_response(self,
                       stream_id: int,
                       status_code: int,
                       end_stream=False) -> None:
        headers = [(b":status", str(status_code).encode())]
        headers.append((b"sec-webtransport-http3-draft", b"draft02"))
        self._http.send_headers(
            stream_id=stream_id, headers=headers, end_stream=end_stream)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('certificate')
    parser.add_argument('key')
    args = parser.parse_args()

    configuration = QuicConfiguration(
        alpn_protocols=H3_ALPN,
        is_client=False,
        max_datagram_frame_size=65536,
        quic_logger=quic_logger,
    )
    configuration.load_cert_chain(args.certificate, args.key)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        serve(
            BIND_ADDRESS,
            BIND_PORT,
            configuration=configuration,
            create_protocol=WebTransportProtocol,
        ))
    try:
        logging.info(
            "Listening on https://{}:{}".format(BIND_ADDRESS, BIND_PORT))
        loop.run_forever()
    except KeyboardInterrupt:
        pass