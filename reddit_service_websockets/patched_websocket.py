"""
This module patches a few core functions to add compression capabilities,
since gevent-websocket does not appear to be maintained anymore.
"""
from socket import error
from zlib import (
    Z_SYNC_FLUSH,
)

from geventwebsocket.exceptions import WebSocketError
from geventwebsocket.websocket import (
    MSG_ALREADY_CLOSED,
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
        # I believe this is a zlib thing but not a deflate thing, so we strip
        # it off the end before passing the message back to the client.
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
        send_frame(websocket, message, opcode, compressor)
        # End patched lines

    except WebSocketError:
        websocket.current_app.on_close(MSG_SOCKET_DEAD)
        raise WebSocketError(MSG_SOCKET_DEAD)
