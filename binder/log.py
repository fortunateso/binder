import logging
import Queue
import time

from threading import Thread, current_thread, Lock

import zmq
from tornado import gen

from binder.binderd.client import BinderClient
from binder.settings import LogSettings

class LoggerClient(Thread):

    _singleton = None

    @staticmethod
    def getInstance():
        if not LoggerClient._singleton:
            client = LoggerClient()
            client.start()
            LoggerClient._singleton = client
        return LoggerClient._singleton

    def __init__(self):
        super(LoggerClient, self).__init__()
        self.parent = current_thread()
        self._stopped = False

        self._queue = Queue.Queue()
        self._client = BinderClient("log_writer")

    def stop(self): 
        self._client.close()
        self._stopped= True

    def _send_message(self):
        msg = self._queue.get()
        self._client.send(msg)

    def run(self):
        while not self._stopped:
            self._send_message()
        # keep logging until the queue is empty, even after the parent has died
        while not self._queue.empty():
            self._send_message()

    def _send(self, level, tag, msg, **kwargs):
        to_send = {'type': 'log', 'level': level, 'msg': msg, 'tag': tag}
        to_send.update(kwargs)
        self._queue.put(to_send)

    def debug(self, tag, msg, **kwargs):
        self._send(logging.DEBUG, tag, msg, **kwargs)

    def info(self, tag, msg, **kwargs):
        self._send(logging.INFO, tag, msg, **kwargs)

    def warn(self, tag, msg, **kwargs):
        self._send(logging.WARNING, tag, msg, **kwargs)

    def error(self, tag, msg, **kwargs):
        self._send(logging.ERROR, tag, msg, **kwargs)


def debug_log(tag, msg, **kwargs):
    log = LoggerClient.getInstance()
    log.debug(tag, msg, **kwargs)

def info_log(tag, msg, **kwargs):
    log = LoggerClient.getInstance()
    log.info(tag, msg, **kwargs)

def warning_log(tag, msg, **kwargs):
    log = LoggerClient.getInstance()
    log.warn(tag, msg, **kwargs)

def error_log(tag, msg, **kwargs):
    log = LoggerClient.getInstance()
    log.error(tag, msg, **kwargs)

def write_stream(tag, level_string, stream, **kwargs):
    def _process_stream(stream):
        log = LoggerClient.getInstance()
        if level_string not in LoggerClient.__dict__: 
            log.error("LoggerClient", "write_stream failing with unexpected level_string: {}".format(level_string))
            return
        method = log.__getattribute__(level_string)
        for line in iter(stream.readline, ''):
            method(tag, line, **kwargs)
    t = Thread(target=_process_stream, args=(stream,))
    t.start()

def get_app_logs(app, start_time, filtered=False):
    lines = []
    bc = BinderClient("log_reader")
    rsp = bc.send({ "type": "get", "app": app, "since": start_time, "filtered": filtered })
    if rsp and rsp["type"] == "success":
        lines = rsp["msg"].split("\n")
    else:
        error_log("LoggerClient", "read_stream failure for app {}: {}".format(app, rsp))
    bc.close()
    return lines


class PubSubStreamer(Thread):

    class SubStreamReader(Thread):

        def __init__(self, buf):
            super(PubSubStreamer.SubStreamReader, self).__init__()
            self._stopped = False
            self._buf = buf

        def stop(self):
            self._stopped = True

        def run(self):
            context = zmq.Context()
            socket = context.socket(zmq.SUB)
            socket.setsockopt(zmq.SUBSCRIBE, b'')
            socket.connect("{}:{}".format(LogSettings.PUBSUB_HOST, LogSettings.PUBSUB_PORT))
            while not self._stopped:
                try:
                    topic, msg = socket.recv_multipart(zmq.NOBLOCK)
                    # buffer the message
                    self._buf.put((topic, msg))
                except zmq.ZMQError:
                    continue

    _singleton = None

    def __init__(self):
        super(PubSubStreamer, self).__init__()
        self._stopped = False
        self._queue = Queue.Queue()
        self._sub_reader = PubSubStreamer.SubStreamReader(self._queue)
        self.callbacks = {}

    @staticmethod
    def get_instance():
        if not PubSubStreamer._singleton: 
            PubSubStreamer._singleton = PubSubStreamer()
            PubSubStreamer._singleton.start()
        return PubSubStreamer._singleton

    def add_app_callback(self, app, cb):
        if app in self.callbacks:
            self.callbacks[app].append(cb)
        else:
            self.callbacks[app] = [cb]

    def stop(self):
        self._stopped = True
        self._sub_reader.stop()

    def remove_app_callback(self, app, cb):
        if app in self.callbacks:
            try: 
                self.callbacks[app].remove(cb)
            except ValueError:
                pass

    def run(self):
        self._sub_reader.start()
        while not self._stopped:
            app, msg = self._queue.get()
            if app in self.callbacks: 
                for cb in self.callbacks[app]:
                    cb(msg)


class AppLogStreamer(object):

    def __init__(self, app, start_time):
        super(AppLogStreamer, self).__init__()
        self.daemon = True
        self._stopped = False
        self._app = app
        self._start_time = start_time
        self._pubsub_cb = None
        PubSubStreamer.get_instance()

    def stop(self): 
        self._stopped = True
        if self._pubsub_cb:
            PubSubStreamer.get_instance().remove_app_callback(self._app, self._pubsub_cb)

    def get_stream(self):
        buf = Queue.Queue()
        def _insert_msg(msg):
            buf.put(msg)
        self._pubsub_cb = _insert_msg
        PubSubStreamer.get_instance().add_app_callback(self._app, self._pubsub_cb)
            
        lines = get_app_logs(self._app, self._start_time, filtered=True)

        # exhaust all lines from the get request
        last_time = None
        for line in lines:
            last_time = LogSettings.EXTRACT_TIME(line)
            yield line
        if last_time:
            last_time = time.strptime(last_time, LogSettings.TIME_FORMAT)
        
        # now start reading the subscriber output (starting strictly after last_time)
        while not self._stopped:
            try: 
                line = buf.get_nowait()
                line_time = time.strptime(LogSettings.EXTRACT_TIME(line), LogSettings.TIME_FORMAT)
                if not last_time or line_time > last_time:
                    yield line
            except Queue.Empty:
                yield None
        raise StopIteration()

