import logging
import urlparse
from zlib import (
    compressobj,
    DEFLATED,
    MAX_WBITS,
)

import gevent
import geventwebsocket
import geventwebsocket.handler

from baseplate.crypto import MessageSigner, SignatureError

from .patched_websocket import send as websocket_send


LOG = logging.getLogger(__name__)


class WebSocketHandler(geventwebsocket.handler.WebSocketHandler):
    def read_request(self, raw_requestline):
        retval = super(WebSocketHandler, self).read_request(raw_requestline)

        # if the client address doesn't exist, we're probably bound to a
        # unix socket and someone else (e.g. nginx) will be forwarding the
        # client's real address.
        if not self.client_address:
            try:
                self.client_address = (
                    self.headers["x-forwarded-for"],
                    int(self.headers["x-forwarded-port"]),
                )
            except KeyError:
                pass

        return retval

    def log_request(self):
        # geventwebsocket logs every request at INFO level. that's annoying.
        pass

    def upgrade_connection(self):
        """Validate authorization to attach to a namespace before connecting.

        We hook into the geventwebsocket stuff here to ensure that the
        authorization is valid before we return a `101 Switching Protocols`
        rather than connecting then disconnecting the user.

        """
        if not self.client_address:
            # we can't do websockets if we don't have a valid client_address
            # because geventwebsocket uses that to keep connection objects.
            # if this error is happening, you probably need to update your proxy
            raise Exception("no client address. check x-forwarded-{for,port}")

        app = self.application

        try:
            namespace = self.environ["PATH_INFO"]
            query_string = self.environ["QUERY_STRING"]
            params = urlparse.parse_qs(query_string, strict_parsing=True)
            signature = params["m"][0]
            app.signer.validate_signature(namespace, signature)
        except (KeyError, IndexError, ValueError, SignatureError):
            app.metrics.counter("conn.rejected.bad_namespace").increment()
            self.start_response("403 Forbidden", [])
            return ["Forbidden"]

        self.environ["signature_validated"] = True

        # Check if compression is supported.  The RFC explanation for the
        # variations on what is accepted here is convoluted, so we'll just
        # stick with the happy case, ignoring arguments to the :
        #
        #    https://tools.ietf.org/html/rfc6455#page-48
        #
        # Worst case scenario, we fall back to not compressing.
        extensions = self.environ.get('HTTP_SEC_WEBSOCKET_EXTENSIONS')
        extensions = {extension.split(";")[0].strip()
                      for extension in extensions.split(",")}
        self.environ["supports_compression"] = \
            "permessage-deflate" in extensions

        return super(WebSocketHandler, self).upgrade_connection()

    def start_response(self, status, headers, exc_info=None):
        # Ideally, this would probably go in `upgrade_connection`, but we have
        # no way to modify the headers there without duplicating the logic of
        # the entire method.  This is a much smaller, cleaner entry point.
        # Ideally, geventwebsocket would just support this stuff.
        if self.environ["supports_compression"]:
            headers.append(("Sec-WebSocket-Extensions", "permessage-deflate"))
        return super(WebSocketHandler, self).start_response(
            status, headers, exc_info=exc_info)


class SocketServer(object):
    def __init__(self, metrics, dispatcher, mac_secret, ping_interval):
        self.metrics = metrics
        self.dispatcher = dispatcher
        self.signer = MessageSigner(mac_secret)
        self.ping_interval = ping_interval
        self.status_publisher = None

    def __call__(self, environ, start_response):
        path_info = environ["PATH_INFO"]
        if path_info == "/health":
            start_response("200 OK", [
                ("Content-Type", "application/json"),
            ])
            return ['{"status": "OK"}']

        websocket = environ.get("wsgi.websocket")
        if not websocket:
            self.metrics.counter("conn.rejected.not_websocket").increment()
            start_response("400 Bad Request", [])
            return ["you are not a websocket"]

        # Setup compression
        if environ["supports_compression"]:
            # See http://www.zlib.net/manual.html#Advanced for details:
            #
            #     "windowBits can also be -8..-15 for raw deflate"
            #
            # Also see http://stackoverflow.com/a/22311297/720638
            compressor = compressobj(7, DEFLATED, -MAX_WBITS)
        else:
            compressor = None

        # ensure the application was properly configured to use the custom
        # handler subclass which validates namespace signatures
        assert environ["signature_validated"]

        namespace = environ["PATH_INFO"]
        dispatcher = gevent.spawn(
            self._pump_dispatcher, namespace, websocket, compressor)

        try:
            self.metrics.counter("conn.connected").increment()
            self._send_message("connect", {"namespace": namespace})
            while True:
                message = websocket.receive()
                if message is None:
                    break
        except geventwebsocket.WebSocketError as e:
            LOG.debug("socket failed: %r", e)
        finally:
            self.metrics.counter("conn.lost").increment()
            self._send_message("disconnect", {"namespace": namespace})
            dispatcher.kill()

    def _send_message(self, key, value):
        if self.status_publisher:
            self.status_publisher("websocket.%s" % key, value)

    def _pump_dispatcher(self, namespace, websocket, compressor=None):
        for msg in self.dispatcher.listen(
                namespace, max_timeout=self.ping_interval):
            if msg is not None:
                websocket_send(websocket, msg, compressor=compressor)
            else:
                websocket.send_frame("", websocket.OPCODE_PING)
