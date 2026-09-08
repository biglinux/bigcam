"""Optional aioquic adapter; all media streams require their CONNECT session."""
from urllib.parse import parse_qs, urlsplit
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import HeadersReceived, DataReceived, WebTransportStreamDataReceived, DatagramReceived
from aioquic.quic.events import ProtocolNegotiated, ConnectionTerminated, StreamReset
from core.phone_protocol import StreamAssembler, valid_token


class PhoneTransport(QuicConnectionProtocol):
    def __init__(self, *args, phone_server, **kwargs):
        super().__init__(*args, **kwargs)
        self.phone = phone_server
        self.h3 = None
        self.assembler = StreamAssembler(None)

    def quic_event_received(self, event):
        if isinstance(event, ConnectionTerminated):
            self.phone.release(self)
            self.assembler.streams.clear()
            return
        if isinstance(event, StreamReset):
            self.assembler.discard(event.stream_id)
            if event.stream_id == self.assembler.session_id:
                self.phone.release(self)
                self.assembler.session_id = None
        if isinstance(event, ProtocolNegotiated):
            self.h3 = H3Connection(self._quic, enable_webtransport=True)
        if self.h3 is not None:
            for item in self.h3.handle_event(event):
                self._event(item)

    def _event(self, event):
        if isinstance(event, HeadersReceived):
            headers = dict(event.headers)
            try:
                parsed = urlsplit(headers.get(b":path", b"").decode("utf-8"))
                token = parse_qs(parsed.query, max_num_fields=8).get("token", [""])[0]
            except (ValueError, UnicodeError):
                token, parsed = "", None
            authorized = (headers.get(b":method") == b"CONNECT" and headers.get(b":protocol") == b"webtransport"
                          and parsed and parsed.path == "/camera" and valid_token(token, self.phone._token))
            if not authorized:
                status = b"401"
            elif self.assembler.session_id is not None or not self.phone.claim(self):
                status = b"409"
            else:
                self.assembler = StreamAssembler(event.stream_id)
                status = b"200"
            self.h3.send_headers(event.stream_id, [(b":status", status)], end_stream=status != b"200")
            self.transmit()
        elif isinstance(event, DataReceived):
            if event.stream_id == self.assembler.session_id and event.stream_ended:
                self.phone.release(self)
                self.assembler.session_id = None
        elif isinstance(event, WebTransportStreamDataReceived):
            try:
                packet = self.assembler.feed(event.session_id, event.stream_id, event.data, event.stream_ended)
                if packet:
                    self.phone.receive(packet, self)
            except (ValueError, PermissionError):
                self.phone.release(self)
                self.close(error_code=0x100, reason_phrase="Invalid camera stream")
        elif isinstance(event, DatagramReceived):
            # PCM uses reliable, sequenced streams, not oversized QUIC datagrams.
            # No unauthenticated datagram can allocate audio resources.
            if event.stream_id != self.assembler.session_id:
                self.close(error_code=0x100, reason_phrase="Unauthorized datagram")
