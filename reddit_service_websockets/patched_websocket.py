"""
This module patches a few core functions to add compression capabilities,
since gevent-websocket does not appear to be maintained anymore.
"""
from socket import error
from zlib import (
    Z_SYNC_FLUSH,
)

from geventwebsocket.exceptions import (
    ProtocolError,
    WebSocketError,
)
from geventwebsocket.websocket import (
    MSG_ALREADY_CLOSED,
    MSG_CLOSED,
    MSG_SOCKET_DEAD,
    Header,
)


def send_frame(websocket, message, opcode, compressor=None):
    # Patched `send_frame` method that supports compression.  `compressor`
    # should be a zlib compressor object.  Patched lines are commented
    # accordingly.

    if websocket.closed:
        websocket.current_app.on_close(MSG_ALREADY_CLOSED)
        raise WebSocketError(MSG_ALREADY_CLOSED)

    if opcode == websocket.OPCODE_TEXT:
        message = websocket._encode_bytes(message)
    elif opcode == websocket.OPCODE_BINARY:
        message = str(message)

    # Start patched lines
    if compressor:
        message = compressor.compress(message)
        message += compressor.flush(Z_SYNC_FLUSH)
        # See https://tools.ietf.org/html/rfc7692#page-19
        if message.endswith('\x00\x00\xff\xff'):
            message = message[:-4]
        flags = Header.RSV0_MASK
    else:
        flags = 0
    header = Header.encode_header(True, opcode, '', len(message), flags)
    # End patched lines

    try:
        websocket.raw_write(header + message)
    except error:
        raise WebSocketError("Socket is dead")


def send(websocket, message, binary=None, compressor=None):
    # Patched `send` method that supports compression.

    if binary is None:
        binary = not isinstance(message, (str, unicode))

    opcode = websocket.OPCODE_BINARY if binary else websocket.OPCODE_TEXT

    try:

        # Start patched lines
        send_frame(websocket, message, opcode, compressor=compressor)
        # End patched lines

    except WebSocketError:
        websocket.current_app.on_close(MSG_SOCKET_DEAD)
        raise WebSocketError(MSG_SOCKET_DEAD)


def read_frame(websocket, decompressor=None):
    # Patched `read_frame` method that supports decompression

    header = Header.decode_header(websocket.stream)

    # Start patched lines
    compressed = decompressor and (header.flags & header.RSV0_MASK)
    if compressed:
        header.flags &= ~header.RSV0_MASK
    # End patched lines

    if header.flags:
        raise ProtocolError

    if not header.length:
        return header, ''

    try:
        payload = websocket.raw_read(header.length)
    except error:
        payload = ''
    except Exception:

        # Start patched lines
        raise WebSocketError('Could not read payload')
        # End patched lines

    if len(payload) != header.length:
        raise WebSocketError('Unexpected EOF reading frame payload')

    if header.mask:
        payload = header.unmask_payload(payload)

    # Start patched lines
    if compressed:
        payload = ''.join((
            decompressor.decompress(payload),
            decompressor.decompress('\0\0\xff\xff'),
            decompressor.flush(),
        ))
    # End patched lines

    return header, payload


def read_message(websocket, decompressor=None):
    opcode = None
    message = ""

    while True:
        header, payload = read_frame(websocket, decompressor=decompressor)
        f_opcode = header.opcode

        if f_opcode in (websocket.OPCODE_TEXT, websocket.OPCODE_BINARY):
            # a new frame
            if opcode:
                raise ProtocolError("The opcode in non-fin frame is "
                                    "expected to be zero, got "
                                    "{0!r}".format(f_opcode))

            # Start reading a new message, reset the validator
            websocket.utf8validator.reset()
            websocket.utf8validate_last = (True, True, 0, 0)

            opcode = f_opcode

        elif f_opcode == websocket.OPCODE_CONTINUATION:
            if not opcode:
                raise ProtocolError("Unexpected frame with opcode=0")

        elif f_opcode == websocket.OPCODE_PING:
            websocket.handle_ping(header, payload)
            continue

        elif f_opcode == websocket.OPCODE_PONG:
            websocket.handle_pong(header, payload)
            continue

        elif f_opcode == websocket.OPCODE_CLOSE:
            websocket.handle_close(header, payload)
            return

        else:
            raise ProtocolError("Unexpected opcode={0!r}".format(f_opcode))

        if opcode == websocket.OPCODE_TEXT:
            websocket.validate_utf8(payload)

        message += payload

        if header.fin:
            break

    if opcode == websocket.OPCODE_TEXT:
        websocket.validate_utf8(message)
        return message
    else:
        return bytearray(message)


def receive(websocket, decompressor=None):

    if websocket.closed:
        websocket.current_app.on_close(MSG_ALREADY_CLOSED)
        raise WebSocketError(MSG_ALREADY_CLOSED)

    try:
        return read_message(websocket, decompressor=decompressor)
    except UnicodeError:
        websocket.logger.debug("exception UnicodeError")
        websocket.close(1007)
    except ProtocolError as e:
        websocket.logger.debug("exception ProtocolError %r", e)
        websocket.close(1002)
    except error:
        websocket.logger.debug("exception socket.error")
        websocket.close()
        websocket.current_app.on_close(MSG_CLOSED)

    return None
