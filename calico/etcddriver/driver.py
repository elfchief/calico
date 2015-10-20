# -*- coding: utf-8 -*-
# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
calico.etcddriver.driver
~~~~~~~~~~~~~~~~~~~~~~~~

Contains the logic for the etcd driver process, which monitors etcd for
changes and sends them to Felix over a unix socket.

The driver is responsible for

* loading the configuration from etcd at start-of-day (Felix needs this before
  it can receive further updates)
* handling the initial load of data from etcd
* watching etcd for changes
* doing the above in parallel and merging the result into a consistent
  sequence of events
* resolving directory deletions so that if a directory is deleted, it tells
  Felix about all the individual keys that are deleted.
"""

from httplib import HTTPException
from json import loads
import socket
import logging
from Queue import Queue, Empty
import sys
from threading import Thread, Event
import time
from urlparse import urlparse

from ijson.backends import yajl2 as ijson
from io import BytesIO
import msgpack
import urllib3
from urllib3 import HTTPConnectionPool
from urllib3.exceptions import ReadTimeoutError

from calico.etcddriver.hwm import HighWaterTracker

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s [%(levelname)s]'
                           '[%(process)s/%(thread)d] %(name)s %(lineno)d: '
                           '%(message)s')
events_processed = 0
snapshot_events = 0
watcher_events = 0
snap_skipped = 0

FLUSH_THRESHOLD = 200

MSG_KEY_TYPE = "type"

# Init message Felix -> Driver.
MSG_TYPE_INIT = "init"
MSG_KEY_ETCD_URL = "etcd_url"

MSG_KEY_LOG_FILE = "log_file"
MSG_KEY_SEV_FILE = "sev_file"
MSG_KEY_SEV_SCREEN = "sev_screen"
MSG_KEY_SEV_SYSLOG = "sev_syslog"

MSG_TYPE_STATUS = "stat"
MSG_TYPE_CONFIG = "conf"
MSG_TYPE_UPDATE = "upd"


def report_status():
    while True:
        start_tot = events_processed
        start_snap = snapshot_events
        start_watch = watcher_events
        start_skip = snap_skipped
        time.sleep(1)
        end_tot = events_processed
        end_snap = snapshot_events
        end_watch = watcher_events
        end_skip = snap_skipped
        _log.info(
            "Events/s: %s Snap: %s, Watch %s, Skip: %s",
            end_tot - start_tot,
            end_snap - start_snap,
            end_watch - start_watch,
            end_skip - start_skip
        )


# etcd response data looks like this:
# {u'action': u'set',
#      u'node': {u'createdIndex': 2095663, u'modifiedIndex': 2095663,
#                u'value': u'{"name": "tap000174", "profile_id": "prof-174", '
#                          u'"state": "active", "ipv6_nets": [], '
#                          u'"mac": "63:4e:60:d9:91:a6", "ipv4_nets": '
#                          u'["1.0.0.174/32"]}',
#                u'key': u'/calico/v1/host/host_bloop/workload/orch/'
#                        u'endpoint_175/endpoint/endpoint_175'},
#      u'prevNode': {u'createdIndex': 2025647, u'modifiedIndex': 2025647,
#                    u'value': u'{"name": "tap000174", "profile_id": '
#                              u'"prof-174", "state": "active", '
#                              u'"ipv6_nets": [], "mac": "37:95:03:e2:f3:6c", '
#                              u'"ipv4_nets": ["1.0.0.174/32"]}',
#                    u'key': u'/calico/v1/host/host_bloop/workload/orch/'
#                            u'endpoint_175/endpoint/endpoint_175'}}


def watch_etcd(next_index, result_queue, stop_event):
    _log.info("Watcher thread started")
    http = None
    try:
        while not stop_event.is_set():
            if not http:
                _log.info("No HTTP pool, creating one...")
                http = HTTPConnectionPool("localhost", 4001, maxsize=1)
            try:
                _log.debug("Waiting on etcd index %s", next_index)
                resp = http.request(
                    "GET",
                    "http://localhost:4001/v2/keys/calico/v1",
                    fields={"recursive": "true",
                            "wait": "true",
                            "waitIndex": next_index},
                    timeout=90,
                )
                resp_body = loads(resp.data)
            except ReadTimeoutError:
                _log.exception("Watch read timed out, restarting watch at "
                               "index %s", next_index)
                # Workaround urllib3 bug #718.  After a ReadTimeout, the
                # connection is incorrectly recycled.
                http = None
                continue
            except:
                _log.exception("Unexpected exception")
                raise
            else:
                node = resp_body["node"]
                key = node["key"]
                value = node.get("value")
                modified_index = node["modifiedIndex"]
                result_queue.put((modified_index, key, value))
                next_index = modified_index + 1
    finally:
        result_queue.put(None)


class EtcdDriver(object):
    def __init__(self, felix_sck):
        self._felix_sck = felix_sck

        # Global stop event used to signal to all threads to stop.
        self._stop_event = Event()

        self._reader_thread = Thread(target=self._read_from_socket)
        self._resync_thread = Thread(target=self._resync_and_merge)

        self._watcher_thread = None  # Created on demand
        self._watcher_stop_event = None

        # High-water mark cache.  Owned by resync thread.
        self._hwms = HighWaterTracker()
        # Number of pending updates and buffer.  Owned by resync thread.
        self._updates_pending = 0
        self._buf = BytesIO()
        self._first_resync = True

        # Set by the reader thread once the config has been read from Felix.
        self._config_loaded = Event()
        self._etcd_base_url = None

    def start(self):
        self._reader_thread.start()
        self._resync_thread.start()

    def join(self):
        self._reader_thread.join()
        self._resync_thread.join()

    def _read_from_socket(self):
        """
        Thread: reader thread.  Reads messages from Felix.

        So far, this means reading the init message and then dealing
        with the exception if Felix dies.
        """
        try:
            unpacker = msgpack.Unpacker()
            while not self._stop_event.is_set():
                try:
                    data = self._felix_sck.recv(8092)
                except:
                    _log.exception("Exception reading from Felix.")
                    raise
                if not data:
                    _log.error("No data read, assuming Felix closed socket")
                    break
                unpacker.feed(data)
                for msg in unpacker:
                    if msg[MSG_KEY_TYPE] == MSG_TYPE_INIT:
                        self._handle_init(msg)
                    else:
                        _log.warning("Unexpected message from Felix")
        finally:
            _log.error("Reader thread shutting down, triggering stop event")
            self._stop_event.set()

    def _handle_init(self, msg):
        # OK to dump the msg, it's a one-off.
        _log.info("Got init message from Felix %s", msg)
        self._etcd_base_url = msg[MSG_KEY_ETCD_URL].rstrip("/")
        self._etcd_url_parts = urlparse(self._etcd_base_url)
        self._config_loaded.set()

    def _resync_and_merge(self):
        """
        Thread: Resync-and-merge thread.  Loads the etcd snapshot, merges
        it with the events going on concurrently and sends the event stream
        to Felix.
        """
        _log.info("Resync thread started, waiting for config to be loaded...")
        self._config_loaded.wait()
        _log.info("Config loaded; continuing.")
        while not self._stop_event.is_set():
            # Only has an effect if it's running.  Note: stopping the watcher
            # is async (and may take a long time for its connection to time
            # out).
            self._stop_watcher()
            # Kick off the request as far as the headers.
            resp, snapshot_index = self._start_snapshot_request()
            # Before reading from the snapshot, start the watcher thread.
            self._start_watcher(snapshot_index)
            # Then plough through the update incrementally.
            try:
                # Incrementally process the snapshot, merging in events from
                # the queue.
                self._process_snapshot_and_events(resp, snapshot_index)
                # Make sure we flush before we wait for events.
                self._flush()
                self._process_events_only()
            except FelixWriteFailed:
                _log.exception("Write to Felix failed; shutting down.")
                self._stop_event.set()
            except WatcherDied:
                _log.warning("Watcher died; resyncing.")
            except (urllib3.exceptions.HTTPError,
                    HTTPException,
                    socket.error) as e:
                _log.error("Request to etcd failed: %r; resyncing.", e)
            except:
                _log.exception("Unexpected exception; shutting down.")
                self._stop_event.set()
                raise
            finally:
                self._first_resync = False

    def _start_snapshot_request(self):
        """
        Issues the HTTP request to etcd to load the snapshot but only
        loads it as far as the headers.
        :return: tuple of response and snapshot's etcd index.
        :raises HTTPException
        :raises HTTPError
        :raises socket.error
        """
        _log.info("Loading snapshot headers...")
        http = self.get_etcd_connection()
        resp = http.request("GET",
                            self._etcd_base_url + "/v2/keys/calico/v1",
                            fields={"recursive": "true"},
                            timeout=120,
                            preload_content=False)
        snapshot_index = int(resp.getheader("x-etcd-index", 1))
        _log.info("Got snapshot headers, snapshot index is %s; starting "
                  "watcher...", snapshot_index)
        return resp, snapshot_index

    def _process_snapshot_and_events(self, etcd_response, snapshot_index):
        """
        Processes the etcd snapshot response incrementally while, concurrently,
        merging in updates from the watcher thread.
        :param etcd_response: file-like object representing the etcd response.
        :param snapshot_index: the etcd index of the response.
        """
        self._hwms.start_tracking_deletions()
        for snap_mod, snap_key, snap_value in parse_snapshot(etcd_response):
            old_hwm = self._hwms.update_hwm(snap_key, snapshot_index)
            if snap_mod > old_hwm:
                # This specific key's HWM is newer than the previous
                # version we've seen, send an update.
                self._queue_update(snap_key, snap_value)

            # After we process an update from the snapshot, process
            # several updates from the watcher queue (if there are
            # any).  We limit the number to ensure that we always
            # finish the snapshot eventually.
            for _ in xrange(100):
                if not self._watcher_queue or self._watcher_queue.empty():
                    # Don't block on the watcher if there's nothing to do.
                    break
                try:
                    self._handle_next_watcher_event()
                except WatcherDied:
                    # Continue processing to ensure that we make
                    # progress.
                    _log.warning("Watcher thread died, continuing "
                                 "with snapshot")
                    break
            if self._stop_event.is_set():
                _log.error("Stop event set, exiting")
                raise Stopped()
        # Save occupancy by throwing away the deletion tracking metadata.
        self._hwms.stop_tracking_deletions()
        # Scan for deletions that happened before the snapshot.  We effectively
        # mark all the values seen in the current snapshot above and then this
        # sweeps the ones we didn't touch.
        self._scan_for_deletions(snapshot_index)

    def _process_events_only(self):
        """
        Loops processing the event stream from the watcher thread and feeding
        it to etcd.
        :raises WatcherDied:
        :raises FelixWriteFailed:
        :raises Stopped:
        """
        _log.info("In sync, now processing events only...")
        while not self._stop_event.is_set():
            self._handle_next_watcher_event()
            self._flush()

    def _scan_for_deletions(self, snapshot_index):
        """
        Scans the high-water mark cache for keys that haven't been seen since
        before the snapshot_index and deletes them.
        """
        if self._first_resync:
            _log.info("First resync: skipping deletion scan")
            return
        # Find any keys that were deleted while we were unable to
        # keep up with etcd.
        _log.info("Scanning for deletions")
        deleted_keys = self._hwms.remove_old_keys(snapshot_index)
        for ev_key in deleted_keys:
            # We didn't see the value during the snapshot or via
            # the event queue.  It must have been deleted.
            self._queue_update(ev_key, None)

    def _handle_next_watcher_event(self):
        """
        Waits for an event on the watcher queue and sends it to Felix.
        :raises Stopped:
        :raises WatcherDied:
        :raises FelixWriteFailed:
        """
        if self._watcher_queue is None:
            raise WatcherDied()
        while not self._stop_event.is_set():
            try:
                event = self._watcher_queue.get(timeout=1)
            except Empty():
                pass
            else:
                break
        else:
            raise Stopped()
        if event is None:
            self._watcher_queue = None
            raise WatcherDied()
        ev_mod, ev_key, ev_val = event
        if ev_val is not None:
            # Normal update.
            self._hwms.update_hwm(ev_key, ev_mod)
            self._queue_update(ev_key, ev_val)
        else:
            # Deletion.
            deleted_keys = self._hwms.store_deletion(ev_key,
                                                     ev_mod)
            for child_key in deleted_keys:
                self._queue_update(child_key, None)

    def _start_watcher(self, snapshot_index):
        """
        Starts the watcher thread, creating its queue and event in the process.
        """
        self._watcher_queue = Queue()
        self._watcher_stop_event = Event()
        # Note: we pass the queue and event in as arguments so that the thread
        # will always access the current queue and event.  If it used self.xyz
        # to access them then an old thread that is shutting down could access
        # a new queue.
        self._watcher_thread = Thread(target=watch_etcd,
                                      args=(snapshot_index + 1,
                                            self._watcher_queue,
                                            self._watcher_stop_event))
        self._watcher_thread.daemon = True
        self._watcher_thread.start()

    def _stop_watcher(self):
        """
        If it's running, signals the watcher thread to stop.
        """
        if self._watcher_stop_event is not None:
            _log.info("Watcher was running before, stopping it")
            self._watcher_stop_event.set()
            self._watcher_stop_event = None

    def get_etcd_connection(self):
        return HTTPConnectionPool(self._etcd_url_parts.hostname,
                                  self._etcd_url_parts.port or 2379,
                                  maxsize=1)

    def _queue_update(self, key, value):
        """
        Queues an update message to Felix.
        :raises FelixWriteFailed:
        """
        self._buf.write(msgpack.dumps((key, value)))
        self._updates_pending += 1
        if self._updates_pending > FLUSH_THRESHOLD:
            self._flush()

    def _flush(self):
        """
        Flushes the write buffer to Felix.
        :raises FelixWriteFailed:
        """
        buf_contents = self._buf.getvalue()
        if buf_contents:
            try:
                self._felix_sck.sendall(buf_contents)
            except socket.error as e:
                _log.exception("Failed to write to Felix socket")
                raise FelixWriteFailed(e)
            self._buf = BytesIO()
        self._updates_pending = 0


def parse_snapshot(resp):
    parser = ijson.parse(resp)  # urllib3 response is file-like.
    stack = []
    frame = Node()
    for prefix, event, value in parser:
        if event == "start_map":
            stack.append(frame)
            frame = Node()
        elif event == "map_key":
            frame.current_key = value
        elif event in ("string", "number"):
            if frame.done:
                continue
            if frame.current_key == "modifiedIndex":
                frame.modifiedIndex = value
            if frame.current_key == "key":
                frame.key = value
            elif frame.current_key == "value":
                frame.value = value
            if (frame.key is not None and
                    frame.value is not None and
                    frame.modifiedIndex is not None):
                frame.done = True
                yield frame.modifiedIndex, frame.key, frame.value
            frame.current_key = None
        elif event == "end_map":
            frame = stack.pop(-1)


class Node(object):
    __slots__ = ("key", "value", "action", "current_key", "modifiedIndex",
                 "done")

    def __init__(self):
        self.modifiedIndex = None
        self.key = None
        self.value = None
        self.action = None
        self.current_key = None
        self.done = False


class WatcherDied(Exception):
    pass


class Stopped(Exception):
    pass


class FelixWriteFailed(Exception):
    pass