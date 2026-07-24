#!/usr/bin/env python3
import socket
import threading
import collections
import sys
import time

LISTEN_HOST = '0.0.0.0'
LISTEN_PORT = 1234
UPSTREAM_HOST = '127.0.0.1'
UPSTREAM_PORT = 1235

SAMPLE_RATE = 250_000
BYTES_PER_SEC = SAMPLE_RATE * 2   # I+Q, 8-bit each

CUSHION_BYTES = 1_500_000     # ~3s of stream; buffer this much before draining to client
MAX_BUFFER_BYTES = 6_000_000  # safety cap so latency can't grow unbounded
CHUNK = 65536
TICK_SECONDS = 0.02
BYTES_PER_TICK = int(BYTES_PER_SEC * TICK_SECONDS)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def handle_client(client_sock, addr):
    log(f"client connected: {addr}")
    try:
        upstream = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=5)
    except OSError as e:
        log(f"failed to connect to upstream rtl_tcp: {e}")
        client_sock.close()
        return
    upstream.settimeout(None)
    client_sock.settimeout(None)

    stop = threading.Event()
    buf = collections.deque()
    buf_bytes = 0
    lock = threading.Condition()

    def upstream_to_buffer():
        nonlocal buf_bytes
        try:
            while not stop.is_set():
                data = upstream.recv(CHUNK)
                if not data:
                    break
                with lock:
                    buf.append(data)
                    buf_bytes += len(data)
                    while buf_bytes > MAX_BUFFER_BYTES and buf:
                        dropped = buf.popleft()
                        buf_bytes -= len(dropped)
                    lock.notify_all()
        except OSError:
            pass
        finally:
            stop.set()
            with lock:
                lock.notify_all()

    def take(n):
        """Pop up to n bytes off the front of buf, across as many chunks as needed."""
        nonlocal buf_bytes
        out = bytearray()
        while n > 0 and buf:
            piece = buf[0]
            if len(piece) <= n:
                out += piece
                buf.popleft()
                buf_bytes -= len(piece)
                n -= len(piece)
            else:
                out += piece[:n]
                buf[0] = piece[n:]
                buf_bytes -= n
                n = 0
        return bytes(out)

    def buffer_to_client():
        nonlocal buf_bytes
        try:
            # wait for initial cushion before starting to drain, so brief upstream
            # stalls later can be absorbed without an audible gap at the client
            with lock:
                while buf_bytes < CUSHION_BYTES and not stop.is_set():
                    lock.wait(timeout=0.5)

            next_tick = time.monotonic()
            while True:
                now = time.monotonic()
                sleep_for = next_tick - now
                if sleep_for > 0:
                    time.sleep(sleep_for)
                next_tick += TICK_SECONDS

                with lock:
                    chunk = take(BYTES_PER_TICK)

                if chunk:
                    client_sock.sendall(chunk)
                elif stop.is_set():
                    break
        except OSError:
            pass
        finally:
            stop.set()
            with lock:
                lock.notify_all()

    def client_to_upstream():
        try:
            while not stop.is_set():
                data = client_sock.recv(CHUNK)
                if not data:
                    break
                upstream.sendall(data)
        except OSError:
            pass
        finally:
            stop.set()
            with lock:
                lock.notify_all()

    t1 = threading.Thread(target=upstream_to_buffer, daemon=True)
    t2 = threading.Thread(target=buffer_to_client, daemon=True)
    t3 = threading.Thread(target=client_to_upstream, daemon=True)
    t1.start(); t2.start(); t3.start()
    t1.join(); t2.join(); t3.join()

    upstream.close()
    client_sock.close()
    log(f"client disconnected: {addr}")


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(1)
    log(f"relay listening on {LISTEN_HOST}:{LISTEN_PORT}, upstream {UPSTREAM_HOST}:{UPSTREAM_PORT}, cushion={CUSHION_BYTES}B")
    while True:
        client_sock, addr = srv.accept()
        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        handle_client(client_sock, addr)


if __name__ == '__main__':
    main()
