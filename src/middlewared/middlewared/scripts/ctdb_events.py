#!/usr/bin/python3

import ctdb
import errno
import fcntl
import json
import os
import time

from copy import deepcopy
from middlewared.client import Client
from middlewared.plugins.cluster_linux.utils import CTDBConfig
from middlewared.plugins.cluster_linux.ctdb_services import CTDB_SERVICE_DEFAULTS
from middlewared.service import MIDDLEWARE_STARTED_SENTINEL_PATH
from pathlib import Path

CTDB_VOL_INFO_FILE = CTDBConfig.CTDB_VOL_INFO_FILE.value
CTDB_RUNDIR = '/var/run/ctdb'
CTDB_MONITOR_FAILED_SENTINEL = os.path.join(CTDB_RUNDIR, '.monitored_failed')


class CtdbEvent:

    def __init__(self, **kwargs):
        self.logger = kwargs.get('logger')
        self.client = None
        self.middleware_started = os.path.exists(MIDDLEWARE_STARTED_SENTINEL_PATH)
        self.clservices = None
        self.pnn = -1
        self.node_status = {}
        self.init_node_status = {}
        self.ctdb_info = None
        self.ctdb_service_file = None

    def __enter__(self):
        return self

    def __exit__(self, typ, value, traceback):
        if self.client:
            self.client.close()

    def init_client(self):
        if not self.middleware_started:
            return False

        try:
            self.client = Client()
            return True
        except Exception:
            self.logger.error("Failed to initialize middleware client", exc_info=True)
            return False

    def check_ctdb_shared_volume(self):
        try:
            self.ctdb_info = json.loads(Path(CTDB_VOL_INFO_FILE).read_text())
        except FileNotFoundError:
            self.ctdb_info = self.client.call('ctdb.shared.volume.config')

        self.ctdb_service_file = os.path.join(self.ctdb_info['mountpoint'], '.clustered_services')
        if os.stat(os.path.join('/cluster', self.ctdb_info['volume_name'])).st_ino != 1:
            raise RuntimeError('ctdb shared volume not mounted')

    def load_service_file(self):
        """
        There are two separate files we read here:
        One is cluster-wide service configuration state:
        /cluster/ctdb_shared_vol/.clustered_services

        This file is read to get configuration, but not
        updated in the event scripts

        The other is a node-specific service status file
        /cluster/ctdb_shared_vol/.clustered_services.{pnn}

        This file is updated with current run state of service
        on every monitoring cycle. This allows any node
        on the server to see a cached copy of run state
        of all nodes (based on last monitoring interval).
        """
        self.pnn = ctdb.Client().pnn
        try:
            with open(self.ctdb_service_file, 'r') as f:
                fcntl.lockf(f.fileno(), fcntl.LOCK_SH)
                try:
                    self.cl_services = json.load(f)
                finally:
                    fcntl.lockf(f.fileno(), fcntl.LOCK_UN)

            try:
                with open(f'{self.ctdb_service_file}.{self.pnn}') as f:
                    self.node_status = json.load(f)
            except (FileNotFoundError, json.decoder.JSONDecodeError):
                self.node_status = {}

        except FileNotFoundError:
            self.cl_services = deepcopy(CTDB_SERVICE_DEFAULTS)

        except OSError as e:
            if e.errno == errno.ENOTCONN:
                self.cl_services = deepcopy(CTDB_SERVICE_DEFAULTS)
                raise RuntimeError(
                    "IO operation on gluster volume fuse mount containing ctdbd metadata "
                    "failed with ENOTCONN. This may indicate that the volume is not mounted "
                    "or the mountpoint is otherwise not healthy."
                )

            self.logger.warning('Failed to load clustered services file', exc_info=True)
            self.cl_services = deepcopy(CTDB_SERVICE_DEFAULTS)

        except Exception:
            self.logger.warning('Failed to load clustered services file', exc_info=True)
            self.cl_services = deepcopy(CTDB_SERVICE_DEFAULTS)

    def init(self):
        """
        Raising exception here will prevent ctdb service
        from starting

        At this point ctdb is not listening on its
        unix domain socket so any ctdb related commands
        and python bindings will fail.

        The glusterfs fuse mount of ctdb_shared_vol is no
        longer needed since a cluster mutex helper is being
        used to maintain the recovery lock.
        """
        if not self.init_client():
            return

        self.client.call('core.ping')

    def setup(self):
        """
        This is triggered once aftter the 'init' event
        has completed.

        Raising exception here will prevent ctdb service
        from starting

        At this point the ctdb unix domain socket is available.
        """
        return

    def startup(self):
        """
        This is triggered after the 'setup' event has
        completed and starts all services managed by ctdb.

        If this fails, CTBDB will retry until it succeeds.
        There is no limit to the times it retires.

        This fails if the ctdb_shared_vol is not mounted.
        """
        if not self.init_client():
            return

        try:
            self.check_ctdb_shared_volume()
        except Exception as e:
            self.client.call('ctdb.event.process', {
                'event': 'STARTUP',
                'status': 'FAILURE',
                'reason': str(e),
            })

            raise

        if not os.path.exists(self.ctdb_service_file):
            return

        self.load_service_file()
        for srv in self.cl_services.values():
            if srv['monitor_enable'] and srv['service_enable']:
                self.client.call('service.restart', srv['name'])

        self.client.call('ctdb.event.process', {
            'event': 'STARTUP',
            'status': 'SUCCESS',
        })

    def shutdown(self):
        """
        This shuts down all services managed by ctdb and is
        triggered by ctdb shutting down.
        """
        return

    def monitor(self):
        if not self.init_client():
            self.logger.debug('middlewared is not ready. Skipping monitoring')
            return

        payload = {
            'event': 'MONITOR',
            'status': 'SUCCESS',
        }

        try:
            self.check_ctdb_shared_volume()
        except Exception as e:
            payload = {
                'event': 'MONITOR',
                'status': 'FAILURE',
                'reason': str(e),
                'service': 'ctdb_shared_volume',
            }

        if not payload['status'] == 'FAILURE':
            try:
                self.load_service_file()
            except RuntimeError as e:
                payload = {
                    'event': 'MONITOR',
                    'status': 'FAILURE',
                    'reason': str(e),
                    'service': 'ctdb_shared_volume',
                }

            self.init_node_status = deepcopy(self.node_status)
            for srv in self.cl_services.values():
                if not srv['monitor_enable']:
                    continue

                srvinfo = self.client.call('service.query', [['service', '=', srv['name']]], {'get': True})
                final_state = 'STOPPED'
                ts = time.clock_gettime(time.CLOCK_REALTIME)
                error = None

                if srv['service_enable']:
                    if srvinfo['state'] != 'RUNNING':
                        self.logger.warning('%s: managed service is not running. Attempting to start.',
                                            srv['name'])
                        try:
                            self.client.call('service.start', srv['name'], {'silent': False})
                            final_state = 'RUNNING'
                        except Exception as e:
                            self.logger.warning('%s: failed to start managed service.',
                                                srv['name'], exc_info=True)

                            error = str(e)
                            errmsg = f'{srv["name"]}: service failed to start: {e}'
                            payload = {
                                'event': 'MONITOR',
                                'status': 'FAILURE',
                                'reason': errmsg,
                                'service': srv['name'],
                            }

                    else:
                        final_state = 'RUNNING'

                    if not srvinfo['enable']:
                        self.client.call('service.update', srvinfo['id'], {'enable': True})

                else:
                    if srvinfo['enable']:
                        self.client.call('service.update', srvinfo['id'], {'enable': False})

                    if srvinfo['state'] == 'RUNNING':
                        self.client.call('service.stop', srv['name'])

                self.node_status[srv['name']] = {
                    'running': final_state == 'RUNNING',
                    'last_check': ts,
                    'error': error
                }

        try:
            initial = {k: v['running'] for k, v in self.init_node_status.items()}
        except Exception:
            self.logger.warning('Unable to parse initial node status', exc_info=True)
            initial = {}

        final = {k: v['running'] for k, v in self.node_status.items()}

        if initial and initial != final:
            # init_node_status may be empty dict if this is first time
            # through. In this case, we don't need to send a status change event
            self.client.call('ctdb.event.process', payload)

        if payload['status'] == 'FAILURE' and payload['service'] == 'ctdb_shared_volume':
            self.client.call('ctdb.event.process', payload)
            raise RuntimeError(payload['reason'])

        else:
            with open(f'{self.ctdb_service_file}.{self.pnn}', 'w') as f:
                fcntl.lockf(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(json.dumps(self.node_status))
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.lockf(f.fileno(), fcntl.LOCK_UN)

    def startrecovery(self):
        """
        This event is triggered every time a database recovery process is
        started. It is rarely used.

        In principle all that is needed here is to pass along to middleware
        that recovery has started so that notification can be sent to any
        clustered event subscribers.
        """
        if not self.init_client():
            return

        try:
            ctdb_status = ctdb.Client().status()
            self.client.call('ctdb.event.process', {
                'event': 'STARTRECOVERY', 'status': 'SUCCESS', 'ctdb_status': ctdb_status
            })
        except Exception:
            pass

    def recovered(self):
        """
        This event is triggered every time a database recovery process is
        completed. It is rarely used.

        In principle all that is needed here is to pass along to middleware
        that recovery has finished so that notification can be sent to any
        clustered event subscribers.
        """
        if not self.init_client():
            return

        try:
            ctdb_status = ctdb.Client().status()
            self.client.call('ctdb.event.process', {
                'event': 'RECOVERED', 'status': 'SUCCESS', 'ctdb_status': ctdb_status
            })
        except Exception:
            pass

    def takeip(self, iface, address, netmask):
        return

    def releaseip(self, iface, address, netmask):
        return

    def updateip(self, old_iface, new_iface, address, netmask):
        return

    def ipreallocated(self):
        """
        This event gets triggered on every node when public IP has changed.

        Failure here causes ip allocation to be retried.
        """
        if not self.init_client():
            return

        try:
            self.client.call('ctdb.event.process', {'event': 'IPREALLOCATED', 'status': 'SUCCESS'})
        except Exception:
            pass
