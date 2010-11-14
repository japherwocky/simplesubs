#!/usr/bin/env python
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""A utility class to write to and read from a non-blocking socket."""

import errno
import logging
import socket

from tornado import ioloop

try:
    import ssl # Python 2.6+
except ImportError:
    ssl = None

class IOStream(object):
    """A utility class to write to and read from a non-blocking socket.

    We support three methods: write(), read_until(), and read_bytes().
    All of the methods take callbacks (since writing and reading are
    non-blocking and asynchronous). read_until() reads the socket until
    a given delimiter, and read_bytes() reads until a specified number
    of bytes have been read from the socket.

    A very simple (and broken) HTTP client using this class:

        import ioloop
        import iostream
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        s.connect(("friendfeed.com", 80))
        stream = IOStream(s)

        def on_headers(data):
            headers = {}
            for line in data.split("\r\n"):
               parts = line.split(":")
               if len(parts) == 2:
                   headers[parts[0].strip()] = parts[1].strip()
            stream.read_bytes(int(headers["Content-Length"]), on_body)

        def on_body(data):
            print data
            stream.close()
            ioloop.IOLoop.instance().stop()

        stream.write("GET / HTTP/1.0\r\n\r\n")
        stream.read_until("\r\n\r\n", on_headers)
        ioloop.IOLoop.instance().start()

    """
    def __init__(self, socket, io_loop=None, max_buffer_size=104857600,
                 read_chunk_size=4096):
        self.socket = socket
        self.socket.setblocking(False)
        self.io_loop = io_loop or ioloop.IOLoop.instance()
        self.max_buffer_size = max_buffer_size
        self.read_chunk_size = read_chunk_size
        self._read_buffer = ""
        self._write_buffer = ""
        self._read_delimiter = None
        self._read_bytes = None
        self._read_callback = None
        self._write_callback = None
        self._close_callback = None
        self._state = self.io_loop.ERROR
        self.io_loop.add_handler(
            self.socket.fileno(), self._handle_events, self._state)

    def read_until(self, delimiter, callback):
        """Call callback when we read the given delimiter."""
        assert not self._read_callback, "Already reading"
        self._read_delimiter = delimiter
        self._read_callback = callback
        while True:
            # See if we've already got the data from a previous read
            if self._read_from_buffer():
                return
            self._check_closed()
            if self._read_to_buffer() == 0:
                break
        self._add_io_state(self.io_loop.READ)

    def read_bytes(self, num_bytes, callback):
        """Call callback when we read the given number of bytes."""
        assert not self._read_callback, "Already reading"
        self._read_bytes = num_bytes
        self._read_callback = callback
        while True:
            if self._read_from_buffer():
                return
            self._check_closed()
            if self._read_to_buffer() == 0:
                break
        self._add_io_state(self.io_loop.READ)

    def write(self, data, callback=None):
        """Write the given data to this stream.

        If callback is given, we call it when all of the buffered write
        data has been successfully written to the stream. If there was
        previously buffered write data and an old write callback, that
        callback is simply overwritten with this new callback.
        """
        self._check_closed()
        self._write_buffer += data
        self._add_io_state(self.io_loop.WRITE)
        self._write_callback = callback

    def set_close_callback(self, callback):
        """Call the given callback when the stream is closed."""
        self._close_callback = callback

    def close(self):
        """Close this stream."""
        if self.socket is not None:
            self.io_loop.remove_handler(self.socket.fileno())
            self.socket.close()
            self.socket = None
            if self._close_callback:
                self._run_callback(self._close_callback)

    def reading(self):
        """Returns true if we are currently reading from the stream."""
        return self._read_callback is not None

    def writing(self):
        """Returns true if we are currently writing to the stream."""
        return len(self._write_buffer) > 0

    def closed(self):
        return self.socket is None

    def _handle_events(self, fd, events):
        if not self.socket:
            logging.warning("Got events for closed stream %d", fd)
            return
        if events & self.io_loop.READ:
            self._handle_read()
        if not self.socket:
            return
        if events & self.io_loop.WRITE:
            self._handle_write()
        if not self.socket:
            return
        if events & self.io_loop.ERROR:
            self.close()
            return
        state = self.io_loop.ERROR
        if self._read_delimiter or self._read_bytes:
            state |= self.io_loop.READ
        if self._write_buffer:
            state |= self.io_loop.WRITE
        if state != self._state:
            self._state = state
            self.io_loop.update_handler(self.socket.fileno(), self._state)

    def _run_callback(self, callback, *args, **kwargs):
        try:
            callback(*args, **kwargs)
        except:
            # Close the socket on an uncaught exception from a user callback
            # (It would eventually get closed when the socket object is
            # gc'd, but we don't want to rely on gc happening before we
            # run out of file descriptors)
            self.close()
            # Re-raise the exception so that IOLoop.handle_callback_exception
            # can see it and log the error
            raise

    def _handle_read(self):
        while True:
            try:
                # Read from the socket until we get EWOULDBLOCK or equivalent.
                # SSL sockets do some internal buffering, and if the data is
                # sitting in the SSL object's buffer select() and friends
                # can't see it; the only way to find out if it's there is to
                # try to read it.
                result = self._read_to_buffer()
            except Exception:
                self.close()
                return
            if result == 0:
                break
            else:
                if self._read_from_buffer():
                    return

    def _read_from_socket(self):
        """Attempts to read from the socket.

        Returns the data read or None if there is nothing to read.
        May be overridden in subclasses.
        """
        try:
            chunk = self.socket.recv(self.read_chunk_size)
        except socket.error, e:
            if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            else:
                raise
        return chunk

    def _read_to_buffer(self):
        """Reads from the socket and appends the result to the read buffer.

        Returns the number of bytes read.  Returns 0 if there is nothing
        to read (i.e. the read returns EWOULDBLOCK or equivalent).  On
        error closes the socket and raises an exception.
        """
        try:
            chunk = self._read_from_socket()
        except socket.error, e:
            # ssl.SSLError is a subclass of socket.error
            logging.warning("Read error on %d: %s",
                            self.socket.fileno(), e)
            self.close()
            raise
        if chunk is None:
            return 0
        self._read_buffer += chunk
        if len(self._read_buffer) >= self.max_buffer_size:
            logging.error("Reached maximum read buffer size")
            self.close()
            raise IOError("Reached maximum read buffer size")
        return len(chunk)

    def _read_from_buffer(self):
        """Attempts to complete the currently-pending read from the buffer.

        Returns True if the read was completed.
        """
        if self._read_bytes:
            if len(self._read_buffer) >= self._read_bytes:
                num_bytes = self._read_bytes
                callback = self._read_callback
                self._read_callback = None
                self._read_bytes = None
                self._run_callback(callback, self._consume(num_bytes))
                return True
        elif self._read_delimiter:
            loc = self._read_buffer.find(self._read_delimiter)
            if loc != -1:
                callback = self._read_callback
                delimiter_len = len(self._read_delimiter)
                self._read_callback = None
                self._read_delimiter = None
                self._run_callback(callback,
                                   self._consume(loc + delimiter_len))
                return True
        return False

    def _handle_write(self):
        while self._write_buffer:
            try:
                num_bytes = self.socket.send(self._write_buffer)
                self._write_buffer = self._write_buffer[num_bytes:]
            except socket.error, e:
                if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    break
                else:
                    logging.warning("Write error on %d: %s",
                                    self.socket.fileno(), e)
                    self.close()
                    return
        if not self._write_buffer and self._write_callback:
            callback = self._write_callback
            self._write_callback = None
            self._run_callback(callback)

    def _consume(self, loc):
        result = self._read_buffer[:loc]
        self._read_buffer = self._read_buffer[loc:]
        return result

    def _check_closed(self):
        if not self.socket:
            raise IOError("Stream is closed")

    def _add_io_state(self, state):
        if not self._state & state:
            self._state = self._state | state
            self.io_loop.update_handler(self.socket.fileno(), self._state)


class SSLIOStream(IOStream):
    """Sets up an SSL connection in a non-blocking manner"""
    def __init__(self, *args, **kwargs):
        super(SSLIOStream, self).__init__(*args, **kwargs)
        self._ssl_accepting = True
        self._do_ssl_handshake()

    def _do_ssl_handshake(self):
        # Based on code from test_ssl.py in the python stdlib
        try:
            self.socket.do_handshake()
        except ssl.SSLError, err:
            if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                self._add_io_state(self.io_loop.READ)
                return
            elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                self._add_io_state(self.io_loop.WRITE)
                return
            elif err.args[0] in (ssl.SSL_ERROR_EOF,
                                 ssl.SSL_ERROR_ZERO_RETURN):
                return self.close()
            raise
        except socket.error, err:
            if err.args[0] == errno.ECONNABORTED:
                return self.close()
        else:
            self._ssl_accepting = False

    def _handle_read(self):
        if self._ssl_accepting:
            self._do_ssl_handshake()
            return
        super(SSLIOStream, self)._handle_read()

    def _handle_write(self):
        if self._ssl_accepting:
            self._do_ssl_handshake()
            return
        super(SSLIOStream, self)._handle_write()

    def _read_from_socket(self):
        try:
            # SSLSocket objects have both a read() and recv() method,
            # while regular sockets only have recv().
            # The recv() method blocks (at least in python 2.6) if it is
            # called when there is nothing to read, so we have to use
            # read() instead.
            chunk = self.socket.read(self.read_chunk_size)
        except ssl.SSLError, e:
            # SSLError is a subclass of socket.error, so this except
            # block must come first.
            if e.args[0] == ssl.SSL_ERROR_WANT_READ:
                return None
            else:
                raise
        except socket.error, e:
            if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            else:
                raise
        return chunk
