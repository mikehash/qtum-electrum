# Electrum - Lightweight Bitcoin Client
# Copyright (c) 2011-2016 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import sys
import os
import json
import errno
import queue
import random
import select
import socket
import threading
import time
from collections import defaultdict
import re
import socks
import ipaddress
import dns
import dns.resolver
from . import bitcoin
from . import blockchain
from . import util
from .util import print_error
from .qtum import *
from . import constants
from .interface import Connection, Interface
from .version import ELECTRUM_VERSION, PROTOCOL_VERSION

NODES_RETRY_INTERVAL = 60
SERVER_RETRY_INTERVAL = 10


def parse_servers(result):
    """ parse servers list into dict format"""
    servers = {}
    for item in result:
        host = item[1]
        out = {}
        version = None
        pruning_level = '-'
        if len(item) > 2:
            for v in item[2]:
                if re.match("[st]\d*", v):
                    protocol, port = v[0], v[1:]
                    if port == '': port = constants.net.DEFAULT_PORTS[protocol]
                    out[protocol] = port
                elif re.match("v(.?)+", v):
                    version = v[1:]
                elif re.match("p\d*", v):
                    pruning_level = v[1:]
                if pruning_level == '': pruning_level = '0'
        if out:
            out['pruning'] = pruning_level
            out['version'] = version
            servers[host] = out
    return servers

def filter_version(servers):
    def is_recent(version):
        try:
            return util.versiontuple(version) >= util.versiontuple(PROTOCOL_VERSION)
        except Exception as e:
            return False
    return {k: v for k, v in servers.items() if is_recent(v.get('version'))}

def filter_protocol(hostmap, protocol = 's'):
    '''Filters the hostmap for those implementing protocol.
    The result is a list in serialized form.'''
    eligible = []
    for host, portmap in hostmap.items():
        port = portmap.get(protocol)
        if port:
            eligible.append(serialize_server(host, port, protocol))
    return eligible

def pick_random_server(hostmap = None, protocol = 's', exclude_set = None):
    if hostmap is None:
        hostmap = constants.net.DEFAULT_SERVERS
    if exclude_set is None:
        exclude_set = set()
    eligible = list(set(filter_protocol(hostmap, protocol)) - exclude_set)
    return random.choice(eligible) if eligible else None

from .simple_config import SimpleConfig

proxy_modes = ['socks4', 'socks5', 'http']


def serialize_proxy(p):
    if not isinstance(p, dict):
        return None
    return ':'.join([p.get('mode'), p.get('host'), p.get('port'), p.get('user', ''), p.get('password', '')])


def deserialize_proxy(s):
    if not isinstance(s, str):
        return None
    if s.lower() == 'none':
        return None
    proxy = { "mode":"socks5", "host":"localhost" }
    args = s.split(':')
    n = 0
    if proxy_modes.count(args[n]) == 1:
        proxy["mode"] = args[n]
        n += 1
    if len(args) > n:
        proxy["host"] = args[n]
        n += 1
    if len(args) > n:
        proxy["port"] = args[n]
        n += 1
    else:
        proxy["port"] = "8080" if proxy["mode"] == "http" else "1080"
    if len(args) > n:
        proxy["user"] = args[n]
        n += 1
    if len(args) > n:
        proxy["password"] = args[n]
    return proxy


def deserialize_server(server_str):
    host, port, protocol = str(server_str).rsplit(':', 2)
    assert protocol in 'st'
    int(port)    # Throw if cannot be converted to int
    return host, port, protocol


def serialize_server(host, port, protocol):
    return str(':'.join([host, port, protocol]))


class GUICallbackProcessor(util.DaemonThread):
    verbosity_filter = 'g'

    def __init__(self):
        util.DaemonThread.__init__(self)
        # callbacks set by the GUI
        self.callbacks = defaultdict(list)      # note: needs self.callback_lock
        self.callback_lock = threading.Lock()

        # new incoming events
        self.event_lock = threading.Lock()
        self.pending_events = defaultdict(list)

    def register_callback(self, callback, events):
        with self.callback_lock:
            for event in events:
                self.callbacks[event].append(callback)

    def unregister_callback(self, callback):
        with self.callback_lock:
            for callbacks in self.callbacks.values():
                if callback in callbacks:
                    callbacks.remove(callback)

    def trigger_callback(self, event, *args):
        with self.event_lock:
            if args not in self.pending_events[event]:
                self.pending_events[event].append(args)

    def run(self):
        # process new incoming callbacks
        while self.is_running():
            with self.event_lock:
                for event, arg_list in self.pending_events.items():
                    callbacks = self.callbacks[event][:]
                    [callback(event, *args) for callback in callbacks for args in arg_list]
                self.pending_events = defaultdict(list)
            time.sleep(0.5)
        self.on_stop()


class Network(util.DaemonThread):
    """The Network class manages a set of connections to remote qtum_electrum
    servers, each connected socket is handled by an Interface() object.
    Connections are initiated by a Connection() thread which stops once
    the connection succeeds or fails.

    Our external API:

    - Member functions get_header(), get_interfaces(), get_local_height(),
          get_parameters(), get_server_height(), get_status_value(),
          is_connected(), set_parameters(), stop()
    """
    verbosity_filter = 'n'

    def __init__(self, config=None):
        if config is None:
            config = {}  # Do not use mutables as default values!
        util.DaemonThread.__init__(self)
        self.config = SimpleConfig(config) if isinstance(config, dict) else config
        self.num_server = 10 if not self.config.get('oneserver') else 0
        self.blockchains = blockchain.read_blockchains(self.config)  # note: needs self.blockchains_lock
        self.print_error("blockchains", self.blockchains.keys())
        self.blockchain_index = config.get('blockchain_index', 0)
        if self.blockchain_index not in self.blockchains.keys():
            self.blockchain_index = 0
        # Server for addresses and transactions
        self.default_server = self.config.get('server', None)
        # Sanitize default server
        if self.default_server:
            try:
                deserialize_server(self.default_server)
            except:
                self.print_error('Warning: failed to parse server-string; falling back to random.')
                self.default_server = None
        if not self.default_server:
            self.default_server = pick_random_server()

        # locks: if you need to take multiple ones, acquire them in the order they are defined here!
        self.interface_lock = threading.RLock()            # <- re-entrant
        self.pending_sends_lock = threading.Lock()
        self.recent_servers_lock = threading.RLock()       # <- re-entrant
        self.subscribed_addresses_lock = threading.Lock()
        self.subscribed_tokens_lock = threading.Lock()
        self.blockchains_lock = threading.Lock()

        self.pending_sends = []
        self.message_id = 0
        self.debug = False
        self.irc_servers = {}  # returned by interface (list from irc)
        self.recent_servers = self.read_recent_servers()  # note: needs self.recent_servers_lock

        self.banner = ''
        self.donation_address = ''
        self.relay_fee = None

        # callbacks passed with subscriptions
        self.subscription_lock = threading.Lock()
        self.subscriptions = defaultdict(list)  # note: needs self.subscription_lock
        self.sub_cache = {}  # note: needs self.interface_lock

        # callbacks set by the GUI
        self.gui_callback_processor = GUICallbackProcessor()
        self.gui_callback_processor.start()

        self.downloading_headers = False

        dir_path = os.path.join(self.config.path, 'certs')
        util.make_dir(dir_path)

        # subscriptions and requests
        self.subscribed_addresses = set()   # note: needs self.subscribed_addresses_lock
        self.subscribed_tokens = set()      # note: needs self.subscribed_tokens_lock

        self.h2addr = {}
        # Requests from client we've not seen a response to
        self.unanswered_requests = {}
        # retry times
        self.server_retry_time = time.time()
        self.nodes_retry_time = time.time()
        # kick off the network.  interface is the main server we are currently
        # communicating with.  interfaces is the set of servers we are connecting
        # to or have an ongoing connection with
        self.interface = None              # note: needs self.interface_lock
        self.interfaces = {}               # note: needs self.interface_lock
        self.auto_connect = self.config.get('auto_connect', True)
        self.connecting = set()
        self.requested_chunks = set()
        self.socket_queue = queue.Queue()
        self.start_network(deserialize_server(self.default_server)[2],
                           deserialize_proxy(self.config.get('proxy')))

    def with_interface_lock(func):
        def func_wrapper(self, *args, **kwargs):
            with self.interface_lock:
                return func(self, *args, **kwargs)
        return func_wrapper

    def with_recent_servers_lock(func):
        def func_wrapper(self, *args, **kwargs):
            with self.recent_servers_lock:
                return func(self, *args, **kwargs)
        return func_wrapper

    def register_callback(self, callback, events):
        self.gui_callback_processor.register_callback(callback, events)

    def unregister_callback(self, callback):
        self.gui_callback_processor.unregister_callback(callback)

    def trigger_callback(self, event, *args):
        self.gui_callback_processor.trigger_callback(event, *args)

    def read_recent_servers(self):
        if not self.config.path:
            return []
        path = os.path.join(self.config.path, "recent_servers")
        try:
            with open(path, "r", encoding='utf-8') as f:
                data = f.read()
                return json.loads(data)
        except:
            return []

    @with_recent_servers_lock
    def save_recent_servers(self):
        if not self.config.path:
            return
        path = os.path.join(self.config.path, "recent_servers")
        s = json.dumps(self.recent_servers, indent=4, sort_keys=True)
        try:
            with open(path, "w", encoding='utf-8') as f:
                f.write(s)
        except:
            pass

    @with_interface_lock
    def get_server_height(self):
        return self.interface.tip if self.interface else 0

    def server_is_lagging(self):
        sh = self.get_server_height()
        if not sh:
            self.print_error('no height for main interface')
            return True
        lh = self.get_local_height()
        result = (lh - sh) > 1
        if result:
            self.print_error('%s is lagging (%d vs %d)' % (self.default_server, sh, lh))
            for k in list(self.blockchains.keys()):
                if not self.blockchains[k].is_valid():
                    for server in list(self.interfaces.keys()):
                        interface = self.interfaces[server]
                        if interface.blockchain and interface.blockchain is self.blockchains[k]:
                            self.close_interface(interface)
                    del self.blockchains[k]
        return result

    def _set_status(self, status):
        self.connection_status = status
        self.notify('status')

    def is_connected(self):
        return self.interface is not None

    def is_connecting(self):
        return self.connection_status == 'connecting'

    @with_interface_lock
    def queue_request(self, method, params, interface=None):
        # If you want to queue a request on any interface it must go
        # through this function so message ids are properly tracked
        if interface is None:
            if self.interface:
                interface = self.interface
            else:
                self.print_error('warning: dropping request', method, params)
                return
        message_id = self.message_id
        self.message_id += 1
        if self.debug:
            self.print_error(interface.host, "-->", method, params, message_id)
        interface.queue_request(method, params, message_id)
        return message_id

    @with_interface_lock
    def send_subscriptions(self):
        assert self.interface
        self.print_error('sending subscriptions to', self.interface.server, len(self.unanswered_requests),
                         len(self.subscribed_addresses), len(self.subscribed_tokens))
        self.sub_cache.clear()
        # Resend unanswered requests
        requests = self.unanswered_requests.values()
        self.unanswered_requests = {}
        for request in requests:
            message_id = self.queue_request(request[0], request[1])
            self.unanswered_requests[message_id] = request
        self.queue_request('server.banner', [])
        # self.queue_request('server.donation_address', [])
        # self.queue_request('server.peers.subscribe', [])
        for i in bitcoin.FEE_TARGETS:
            self.queue_request('blockchain.estimatefee', [i])
        self.queue_request('blockchain.relayfee', [])
        with self.subscribed_addresses_lock:
            for h in self.subscribed_addresses:
                self.queue_request('blockchain.scripthash.subscribe', [h])
        with self.subscribed_tokens_lock:
            for hash160, contract_addr, topic in self.subscribed_tokens:
                self.queue_request('blockchain.contract.event.subscribe', [hash160, contract_addr, topic])

    def get_status_value(self, key):
        if key == 'status':
            value = self.connection_status
        elif key == 'banner':
            value = self.banner
        elif key == 'fee':
            value = self.config.fee_estimates
        elif key == 'fee_histogram':
            value = self.config.mempool_fees
        elif key == 'servers':
            value = self.get_servers()
        else:
            raise Exception('unexpected trigger key {}'.format(key))
        return value

    def notify(self, key):
        if key in ['status', 'updated']:
            self.trigger_callback(key)
        else:
            self.trigger_callback(key, self.get_status_value(key))

    def get_parameters(self):
        host, port, protocol = deserialize_server(self.default_server)
        return host, port, protocol, self.proxy, self.auto_connect

    def get_donation_address(self):
        if self.is_connected():
            return self.donation_address

    @with_interface_lock
    def get_interfaces(self):
        '''The interfaces that are in connected state'''
        return list(self.interfaces.keys())

    @with_recent_servers_lock
    def get_servers(self):
        out = dict(constants.net.DEFAULT_SERVERS)  # copy
        for s in self.recent_servers:
            try:
                host, port, protocol = deserialize_server(s)
            except:
                continue
            if host not in out:
                out[host] = {protocol: port}
        return out

    @with_interface_lock
    def start_interface(self, server):
        if (not server in self.interfaces and not server in self.connecting):
            if server == self.default_server:
                self.print_error("connecting to %s as new interface" % server)
                self._set_status('connecting')
            self.connecting.add(server)
            c = Connection(server, self.socket_queue, self.config.path)

    def start_random_interface(self):
        with self.interface_lock:
            exclude_set = self.disconnected_servers.union(set(self.interfaces))
        server = pick_random_server(self.get_servers(), self.protocol, exclude_set)
        if server:
            self.start_interface(server)

    def start_interfaces(self):
        self.start_interface(self.default_server)
        for i in range(self.num_server - 1):
            self.start_random_interface()

    def set_proxy(self, proxy):
        self.proxy = proxy
        # Store these somewhere so we can un-monkey-patch
        if not hasattr(socket, "_socketobject"):
            socket._socketobject = socket.socket
            socket._getaddrinfo = socket.getaddrinfo
        if proxy:
            self.print_error('setting proxy', proxy)
            proxy_mode = proxy_modes.index(proxy["mode"]) + 1
            socks.setdefaultproxy(proxy_mode,
                                  proxy["host"],
                                  int(proxy["port"]),
                                  # socks.py seems to want either None or a non-empty string
                                  username=(proxy.get("user", "") or None),
                                  password=(proxy.get("password", "") or None))
            socket.socket = socks.socksocket
            # prevent dns leaks, see http://stackoverflow.com/questions/13184205/dns-over-proxy
            socket.getaddrinfo = lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (args[0], args[1]))]
        else:
            socket.socket = socket._socketobject
            if sys.platform == 'win32':
                # On Windows, socket.getaddrinfo takes a mutex, and might hold it for up to 10 seconds
                # when dns-resolving. To speed it up drastically, we resolve dns ourselves, outside that lock.
                # see #4421
                socket.getaddrinfo = self._fast_getaddrinfo
            else:
                socket.getaddrinfo = socket._getaddrinfo

    @staticmethod
    def _fast_getaddrinfo(host, *args, **kwargs):
        def needs_dns_resolving(host2):
            try:
                ipaddress.ip_address(host2)
                return False  # already valid IP
            except ValueError:
                pass  # not an IP
            if str(host) in ('localhost', 'localhost.',):
                return False
            return True
        try:
            if needs_dns_resolving(host):
                answers = dns.resolver.query(host)
                addr = str(answers[0])
            else:
                addr = host
        except dns.exception.DNSException:
            # dns failed for some reason, e.g. dns.resolver.NXDOMAIN
            # this is normal. Simply report back failure:
            raise socket.gaierror(11001, 'getaddrinfo failed')
        except BaseException as e:
            # Possibly internal error in dnspython :( see #4483
            # Fall back to original socket.getaddrinfo to resolve dns.
            print_error('dnspython failed to resolve dns with error:', e)
            addr = host
        return socket._getaddrinfo(addr, *args, **kwargs)

    @with_interface_lock
    def start_network(self, protocol, proxy):
        assert not self.interface and not self.interfaces
        assert not self.connecting and self.socket_queue.empty()
        self.print_error('starting network')
        self.disconnected_servers = set([])  # note: needs self.interface_lock
        self.protocol = protocol
        self.set_proxy(proxy)
        self.start_interfaces()
        self.gui_callback_processor.trigger_callback('network_updated')

    @with_interface_lock
    def stop_network(self):
        self.print_error("stopping network")
        for interface in list(self.interfaces.values()):
            self.close_interface(interface)
        if self.interface:
            self.close_interface(self.interface)
        assert self.interface is None
        assert not self.interfaces
        self.connecting = set()
        # Get a new queue - no old pending connections thanks!
        self.socket_queue = queue.Queue()
        self.gui_callback_processor.trigger_callback('network_updated')

    def set_parameters(self, host, port, protocol, proxy, auto_connect):
        proxy_str = serialize_proxy(proxy)
        server = serialize_server(host, port, protocol)
        # sanitize parameters
        try:
            deserialize_server(serialize_server(host, port, protocol))
            if proxy:
                proxy_modes.index(proxy["mode"]) + 1
                int(proxy['port'])
        except:
            return
        self.config.set_key('auto_connect', auto_connect, False)
        self.config.set_key("proxy", proxy_str, False)
        self.config.set_key("server", server, True)
        # abort if changes were not allowed by config
        if self.config.get('server') != server or self.config.get('proxy') != proxy_str:
            return
        self.auto_connect = auto_connect
        if self.proxy != proxy or self.protocol != protocol:
            # Restart the network defaulting to the given server
            with self.interface_lock:
                self.stop_network()
                self.default_server = server
                self.start_network(protocol, proxy)
        elif self.default_server != server:
            self.switch_to_interface(server)
        else:
            self.switch_lagging_interface()

    def switch_to_random_interface(self):
        '''Switch to a random connected server other than the current one'''
        servers = self.get_interfaces()    # Those in connected state
        if self.default_server in servers:
            servers.remove(self.default_server)
        if servers:
            self.switch_to_interface(random.choice(servers))

    @with_interface_lock
    def switch_lagging_interface(self):
        '''If auto_connect and lagging, switch interface'''
        if self.auto_connect and self.server_is_lagging():
            # switch to one that has the correct header (not height)
            best_header = self.blockchain().read_header(self.get_local_height())
            filtered = list(map(lambda x:x[0], filter(lambda x: x[1].tip_header == best_header, self.interfaces.items())))
            if filtered:
                choice = random.choice(filtered)
                self.switch_to_interface(choice)

    @with_interface_lock
    def switch_to_interface(self, server):
        '''Switch to server as our interface.  If no connection exists nor
        being opened, start a thread to connect.  The actual switch will
        happen on receipt of the connection notification.  Do nothing
        if server already is our interface.'''
        self.default_server = server
        if server not in self.interfaces:
            self.interface = None
            self.start_interface(server)
            return
        i = self.interfaces[server]
        if self.interface != i:
            self.print_error("switching to", server)
            blockchain_updated = False

            if self.interface is not None:
                blockchain_updated = i.blockchain != self.interface.blockchain
                # Stop any current interface in order to terminate subscriptions,
                # and to cancel tasks in interface.group.
                # However, for headers sub, give preference to this interface
                # over unknown ones, i.e. start it again right away.
                old_server = self.interface.server
                self.close_interface(self.interface)
                if old_server != server and len(self.interfaces) <= self.num_server:
                    self.start_interface(old_server)

            self.interface = i
            self.send_subscriptions()
            self._set_status('connected')
            self.trigger_callback('network_updated')
            if blockchain_updated: self.trigger_callback('blockchain_updated')

    @with_interface_lock
    def close_interface(self, interface):
        if interface:
            if interface.server in self.interfaces:
                self.interfaces.pop(interface.server)
            if interface.server == self.default_server:
                self.interface = None
            interface.close()

    @with_recent_servers_lock
    def add_recent_server(self, server):
        # list is ordered
        if server in self.recent_servers:
            self.recent_servers.remove(server)
        self.recent_servers.insert(0, server)
        self.recent_servers = self.recent_servers[0:20]
        self.save_recent_servers()

    def process_response(self, interface, response, callbacks):
        if self.debug:
            self.print_error(interface.host, "<--", response)
        error = response.get('error')
        result = response.get('result')
        method = response.get('method')
        params = response.get('params')

        # We handle some responses; return the rest to the client.
        if method == 'server.version':
            interface.server_version = result
        elif method == 'blockchain.headers.subscribe':
            if error is None:
                self.on_notify_header(interface, result)
            else:
                # no point in keeping this connection without headers sub
                self.connection_down(interface.server)
                return
        elif method == 'server.peers.subscribe':
            if error is None:
                self.irc_servers = parse_servers(result)
                self.notify('servers')
        elif method == 'server.banner':
            if error is None:
                self.banner = result
                self.notify('banner')
        elif method == 'server.donation_address':
            if error is None:
                self.donation_address = result
        elif method == 'blockchain.estimatefee':
            if error is None and result is not None and result > 0:
                i = params[0]
                fee = int(result*COIN)
                self.config.fee_estimates[i] = fee
                self.print_error("fee_estimates[%d]" % i, fee)
                self.notify('fee')
        elif method == 'blockchain.relayfee':
            if error is None and result is not None and result > 0:
                self.relay_fee = int(result * COIN)
                self.print_error("relayfee", self.relay_fee)
        elif method == 'blockchain.block.headers':
            self.on_block_headers(interface, response)
        elif method == 'blockchain.block.header':
            if error is None and result is not None:
                header = blockchain.deserialize_header(bfh(result), params[0])
                response = {
                    'result': header,
                }
                self.on_get_header(interface, response)
        for callback in callbacks:
            callback(response)

    @classmethod
    def get_index(cls, method, params):
        """ hashable index for subscriptions and cache"""
        if method == 'blockchain.contract.event.subscribe':
            return '{}:{}:{}:{}'.format(method, params[0], params[1], params[2])
        return str(method) + (':' + str(params[0]) if params else '')

    def process_responses(self, interface):
        responses = interface.get_responses()
        for request, response in responses:
            if request:
                method, params, message_id = request
                k = self.get_index(method, params)
                # client requests go through self.send() with a
                # callback, are only sent to the current interface,
                # and are placed in the unanswered_requests dictionary
                client_req = self.unanswered_requests.pop(message_id, None)
                if client_req:
                    if interface != self.interface:
                        # we probably changed the current interface
                        # in the meantime; drop this.
                        return
                    callbacks = [client_req[2]]
                else:
                    # fixme: will only work for subscriptions
                    k = self.get_index(method, params)
                    callbacks = list(self.subscriptions.get(k, []))

                # Copy the request method and params to the response
                response['method'] = method
                response['params'] = params
                # Only once we've received a response to an addr subscription
                # add it to the list; avoids double-sends on reconnection
                if method == 'blockchain.scripthash.subscribe':
                    with self.subscribed_addresses_lock:
                        self.subscribed_addresses.add(params[0])
                elif method == 'blockchain.contract.event.subscribe':
                    with self.subscribed_tokens_lock:
                        self.subscribed_tokens.add((params[0], params[1], params[2]))
            else:
                if not response:  # Closed remotely / misbehaving
                    self.connection_down(interface.server)
                    break
                # Rewrite response shape to match subscription request response
                method = response.get('method')
                params = response.get('params')
                k = self.get_index(method, params)
                if method == 'blockchain.headers.subscribe':
                    response['result'] = params[0]
                    response['params'] = []
                elif method == 'blockchain.scripthash.subscribe':
                    response['params'] = [params[0]]  # addr
                    response['result'] = params[1]
                elif method == 'blockchain.contract.event.subscribe':
                    response['params'] = params[0:3]  # addr, contract, topic
                    response['result'] = params[3]
                callbacks = list(self.subscriptions.get(k, []))

            # update cache if it's a subscription
            if method.endswith('.subscribe'):
                with self.interface_lock:
                    self.sub_cache[k] = response
            # Response is now in canonical form
            self.process_response(interface, response, callbacks)

    def send(self, messages, callback):
        '''Messages is a list of (method, params) tuples'''
        messages = list(messages)
        with self.pending_sends_lock:
            self.pending_sends.append((messages, callback))

    @with_interface_lock
    def process_pending_sends(self):
        # Requests needs connectivity.  If we don't have an interface,
        # we cannot process them.
        if not self.interface:
            return

        with self.pending_sends_lock:
            sends = self.pending_sends
            self.pending_sends = []

        for messages, callback in sends:
            for method, params in messages:
                r = None
                if method.endswith('.subscribe'):
                    k = self.get_index(method, params)
                    # add callback to list
                    l = list(self.subscriptions.get(k, []))
                    if callback not in l:
                        l.append(callback)
                    with self.subscription_lock:
                        self.subscriptions[k] = l
                    # check cached response for subscriptions
                    r = self.sub_cache.get(k)
                if r is not None and not method.endswith('contract.subscribe'):
                    self.print_error("cache hit", k)
                    callback(r)
                else:
                    message_id = self.queue_request(method, params)
                    self.unanswered_requests[message_id] = method, params, callback

    def unsubscribe(self, callback):
        '''Unsubscribe a callback to free object references to enable GC.'''
        # Note: we can't unsubscribe from the server, so if we receive
        # subsequent notifications process_response() will emit a harmless
        # "received unexpected notification" warning
        with self.subscription_lock:
            for v in self.subscriptions.values():
                if callback in v:
                    v.remove(callback)

    @with_interface_lock
    def connection_down(self, server):
        '''A connection to server either went down, or was never made.
        We distinguish by whether it is in self.interfaces.'''
        self.disconnected_servers.add(server)
        if server == self.default_server:
            self._set_status('disconnected')
        if server in self.interfaces:
            self.close_interface(self.interfaces[server])
            self.trigger_callback('network_updated')
        with self.blockchains_lock:
            for b in self.blockchains.values():
                if b.catch_up == server:
                    b.catch_up = None

    def new_interface(self, server, socket):
        interface = Interface(server, socket)
        interface.blockchain = None
        interface.tip_header = None
        interface.tip = 0
        interface.mode = 'default'
        interface.request = None
        with self.interface_lock:
            self.interfaces[server] = interface
        # server.version should be the first message
        self.queue_request('server.version', [ELECTRUM_VERSION, PROTOCOL_VERSION], interface)
        self.queue_request('blockchain.headers.subscribe', [True], interface)
        if server == self.default_server:
            self.switch_to_interface(server)
        self.add_recent_server(server)

    def maintain_sockets(self):
        '''Socket maintenance.'''
        # Responses to connection attempts?
        while not self.socket_queue.empty():
            server, socket = self.socket_queue.get()
            if server in self.connecting:
                self.connecting.remove(server)
            if socket:
                self.new_interface(server, socket)
            else:
                self.connection_down(server)

        # Send pings and shut down stale interfaces
        # must use copy of values
        with self.interface_lock:
            interfaces = list(self.interfaces.values())
        for interface in interfaces:
            if interface.has_timed_out():
                self.connection_down(interface.server)
            elif interface.ping_required():
                self.queue_request('server.ping', [], interface)

        now = time.time()
        # nodes
        with self.interface_lock:
            if len(self.interfaces) + len(self.connecting) < self.num_server:
                self.start_random_interface()
                if now - self.nodes_retry_time > NODES_RETRY_INTERVAL:
                    self.print_error('network: retrying connections')
                    self.disconnected_servers = set([])
                    self.nodes_retry_time = now

        # main interface
        with self.interface_lock:
            if not self.is_connected():
                if self.auto_connect:
                    if not self.is_connecting():
                        self.switch_to_random_interface()
                else:
                    if self.default_server in self.disconnected_servers:
                        if now - self.server_retry_time > SERVER_RETRY_INTERVAL:
                            self.disconnected_servers.remove(self.default_server)
                            self.server_retry_time = now
                    else:
                        self.switch_to_interface(self.default_server)

    def request_chunk(self, interface, index):
        interface.print_error("requesting chunk %d" % index)
        if index in self.requested_chunks:
            interface.print_error("index {} already in requested chunks".format(index))
            return
        height = index * CHUNK_SIZE
        self.queue_request('blockchain.block.headers', [height, CHUNK_SIZE],
                           interface)
        self.requested_chunks.add(index)

    def on_block_headers(self, interface, response):
        '''Handle receiving a chunk of block headers'''
        error = response.get('error')
        result = response.get('result')
        params = response.get('params')
        blockchain = interface.blockchain
        if result is None or params is None or error is not None:
            interface.print_error('on get chunk error', error, result, params)
            self.switch_to_random_interface()
            if error.get('code') == -101:
                blockchain.catch_up = None
                self.requested_chunks.remove(params[0] // CHUNK_SIZE)
            return

        height = params[0]
        index = height // CHUNK_SIZE
        if index * CHUNK_SIZE != height or index not in self.requested_chunks:
            interface.print_error("received chunk %d (unsolicited)" % index)
            return
        else:
            interface.print_error("received chunk %d" % index)
        self.requested_chunks.remove(index)
        hexdata = result['hex']
        connect = blockchain.connect_chunk(index, hexdata)
        if not connect:
            self.connection_down(interface.server)
            return
        # If not finished, get the next chunk
        if blockchain.height() < interface.tip:
            self.request_chunk(interface, index+1)
        else:
            interface.mode = 'default'
            interface.print_error('catch up done', blockchain.height())
            blockchain.catch_up = None

    def on_get_header(self, interface, response):
        '''Handle receiving a single block header'''
        header = response.get('result')
        if not header:
            interface.print_error(response)
            self.connection_down(interface.server)
            return
        height = header.get('block_height')
        # print_error('[on_get_header] {} {}'.format(height, interface.mode))
        if interface.request != height:
            interface.print_error("unsolicited header", interface.request, height)
            self.connection_down(interface.server)
            return

        chain = blockchain.check_header(header)

        if interface.mode == 'backward':
            can_connect = blockchain.can_connect(header)
            if can_connect and can_connect.catch_up is None:
                interface.mode = 'catch_up'
                interface.blockchain = can_connect
                interface.blockchain.save_header(header)
                next_height = height + 1
                interface.blockchain.catch_up = interface.server
                self.trigger_callback('blockchain_updated')
            elif chain:
                interface.print_error("binary search")
                interface.mode = 'binary'
                interface.blockchain = chain
                interface.good = height
                next_height = (interface.bad + interface.good) // 2
            else:
                if height == 0:
                    self.connection_down(interface.server)
                    next_height = None
                else:
                    interface.bad = height
                    interface.bad_header = header
                    delta = interface.tip - height
                    next_height = max(0, interface.tip - 2 * delta)

        elif interface.mode == 'binary':
            if chain:
                interface.good = height
                interface.blockchain = chain
            else:
                interface.bad = height
                interface.bad_header = header

            if interface.bad != interface.good + 1:
                next_height = (interface.bad + interface.good) // 2
            elif not interface.blockchain.can_connect(interface.bad_header, check_height=False):
                self.connection_down(interface.server)
                next_height = None
            else:
                branch = self.blockchains.get(interface.bad)
                if branch is not None:
                    if branch.check_header(interface.bad_header):
                        interface.print_error('joining chain', interface.bad)
                        next_height = None
                    elif branch.parent().check_header(header):
                        interface.print_error('reorg', interface.bad, interface.tip)
                        interface.blockchain = branch.parent()
                        next_height = interface.bad
                    else:
                        interface.print_error('forkpoint conflicts with existing fork', branch.path())
                        branch.write(b'', 0)
                        branch.save_header(interface.bad_header)
                        interface.mode = 'catch_up'
                        interface.blockchain = branch
                        next_height = interface.bad + 1
                        interface.blockchain.catch_up = interface.server
                        self.trigger_callback('blockchain_updated')
                else:
                    bh = interface.blockchain.height()
                    next_height = None
                    if bh > interface.good:
                        if not interface.blockchain.check_header(interface.bad_header):
                            b = interface.blockchain.fork(interface.bad_header)
                            with self.blockchains_lock:
                                self.blockchains[interface.bad] = b
                            interface.blockchain = b
                            interface.print_error("new chain", b.forkpoint)
                            interface.mode = 'catch_up'
                            next_height = interface.bad + 1
                            interface.blockchain.catch_up = interface.server
                            self.trigger_callback('blockchain_updated')
                    else:
                        assert bh == interface.good
                        if interface.blockchain.catch_up is None and bh < interface.tip:
                            interface.print_error("catching up from %d"% (bh + 1))
                            interface.mode = 'catch_up'
                            next_height = bh + 1
                            interface.blockchain.catch_up = interface.server

        elif interface.mode == 'catch_up':
            can_connect = interface.blockchain.can_connect(header)
            if can_connect:
                interface.blockchain.save_header(header)
                next_height = height + 1 if height < interface.tip else None
                self.trigger_callback('blockchain_updated')
            else:
                # go back
                interface.print_error("cannot connect", height)
                interface.mode = 'backward'
                interface.bad = height
                interface.bad_header = header
                next_height = height - 1

            if next_height is None:
                # exit catch_up state
                interface.print_error('catch up done', interface.blockchain.height())
                interface.blockchain.catch_up = None
                self.switch_lagging_interface()
        else:
            raise Exception(interface.mode)

        # If not finished, get the next header
        if next_height is not None:
            if next_height < 0:
                self.connection_down(interface.server)
                next_height = None
            elif interface.mode == 'catch_up' and interface.tip > next_height + 50:
                self.request_chunk(interface, next_height // CHUNK_SIZE)
            else:
                self.request_header(interface, next_height)

        if next_height is None:
            interface.mode = 'default'
            interface.request = None

    def maintain_requests(self):
        with self.interface_lock:
            interfaces = list(self.interfaces.values())
        for interface in interfaces:
            if interface.request and time.time() - interface.request_time > 30:
                interface.print_error("blockchain request timed out")
                self.connection_down(interface.server)
                continue

    def wait_on_sockets(self):
        # Python docs say Windows doesn't like empty selects.
        # Sleep to prevent busy looping
        if not self.interfaces:
            time.sleep(0.1)
            return
        with self.interface_lock:
            interfaces = list(self.interfaces.values())
        rin = [i for i in interfaces]
        win = [i for i in interfaces if i.num_requests()]
        try:
            rout, wout, xout = select.select(rin, win, [], 0.1)
        except (socket.error, OSError) as e:
            print_error('[wait_on_sockets]', e)
            if e.errno == errno.EINTR:
                return
            raise
        assert not xout
        for interface in wout:
            interface.send_requests()
        for interface in rout:
            self.process_responses(interface)

    def init_headers_file(self):
        pass
        # b = self.blockchains[0]
        # if b.get_hash(0) == bitcoin.GENESIS:
        #     self.downloading_headers = False
        #     return
        # filename = b.path()
        # def download_thread():
        #     try:
        #         import urllib.request, socket
        #         socket.setdefaulttimeout(30)
        #         self.print_error("downloading ", bitcoin.HEADERS_URL)
        #         urllib.request.urlretrieve(bitcoin.HEADERS_URL, filename + '.tmp')
        #         os.rename(filename + '.tmp', filename)
        #         self.print_error("done.")
        #     except Exception:
        #         self.print_error("download failed. creating file", filename)
        #         # open(filename, 'wb+').close()
        #     b = self.blockchains[0]
        #     with b.lock: b.update_size()
        #     self.downloading_headers = False
        # self.downloading_headers = True
        # t = threading.Thread(target = download_thread)
        # t.daemon = True
        # t.start()

    def run(self):
        # self.init_headers_file()
        #
        # while self.is_running() and self.downloading_headers:
        #     time.sleep(1)
        while self.is_running():
            self.maintain_sockets()
            self.wait_on_sockets()
            self.maintain_requests()
            self.run_jobs()    # Synchronizer and Verifier
            self.process_pending_sends()
        self.stop_network()
        self.on_stop()

    def on_notify_header(self, interface, header_dict):
        try:
            header_hex, height = header_dict['hex'], header_dict['height']
        except KeyError:
            # no point in keeping this connection without headers sub
            self.connection_down(interface.server)
            return
        header = blockchain.deserialize_header(bfh(header_hex), height)
        height = header.get('block_height')
        if not height:
            return
        interface.tip_header = header
        interface.tip = height
        if interface.mode != 'default':
            return
        self.trigger_callback('network_updated')

        b = blockchain.check_header(header)
        if b:
            interface.blockchain = b
            self.switch_lagging_interface()
            return
        b = blockchain.can_connect(header)
        if b:
            interface.blockchain = b
            b.save_header(header)
            self.switch_lagging_interface()
            self.trigger_callback('blockchain_updated')
            return
        with self.blockchains_lock:
            tip = max([x.height() for x in self.blockchains.values()])
        if tip >= 0:
            interface.mode = 'backward'
            interface.bad = height
            interface.bad_header = header
            self.request_header(interface, min(tip, height - 1))
        else:
            # todo: why set mode to catch_up here ?
            chain = self.blockchains[0]
            if chain.catch_up is None:
                chain.catch_up = interface.server
                interface.mode = 'catch_up'
                interface.blockchain = chain
                with self.blockchains_lock:
                    self.print_error("switching to catchup mode", tip, self.blockchains)
                self.request_header(interface, 0)
            else:
                self.print_error("chain already catching up with", chain.catch_up)

    @with_interface_lock
    def blockchain(self):
        if self.interface and self.interface.blockchain is not None:
            self.blockchain_index = self.interface.blockchain.forkpoint
        return self.blockchains[self.blockchain_index]

    @with_interface_lock
    def get_blockchains(self):
        out = {}
        with self.blockchains_lock:
            blockchain_items = list(self.blockchains.items())
        for k, b in blockchain_items:
            r = list(filter(lambda i: i.blockchain == b, list(self.interfaces.values())))
            if r:
                out[k] = r
        return out

    def follow_chain(self, index):
        blockchain = self.blockchains.get(index)
        if blockchain:
            self.blockchain_index = index
            self.config.set_key('blockchain_index', index)
            with self.interface_lock:
                interfaces = list(self.interfaces.values())
            for i in interfaces:
                if i.blockchain == blockchain:
                    self.switch_to_interface(i.server)
                    break
        else:
            raise Exception('blockchain not found', index)

        with self.interface_lock:
            if self.interface:
                server = self.interface.server
                host, port, protocol, proxy, auto_connect = self.get_parameters()
                host, port, protocol = server.split(':')
                self.set_parameters(host, port, protocol, proxy, auto_connect)

    def get_local_height(self):
        self.blockchain().update_size()
        return self.blockchain().height()

    @staticmethod
    def __wait_for(it):
        """Wait for the result of calling lambda `it`."""
        q = queue.Queue()
        it(q.put)
        try:
            result = q.get(block=True, timeout=30)
        except queue.Empty:
            raise util.TimeoutException('Server did not answer')

        if result.get('error'):
            raise Exception(result.get('error'))

        return result.get('result')

    @staticmethod
    def __with_default_synchronous_callback(invocation, callback):
        """ Use this method if you want to make the network request
        synchronous. """
        if not callback:
            return Network.__wait_for(invocation)

        invocation(callback)

    def request_header(self, interface, height):
        self.queue_request('blockchain.block.header', [height], interface)
        interface.request = height
        interface.req_time = time.time()

    def map_scripthash_to_address(self, callback):
        def cb2(x):
            x2 = x.copy()
            p = x2.pop('params')
            addr = self.h2addr[p[0]]
            x2['params'] = [addr]
            callback(x2)
        return cb2

    def subscribe_to_addresses(self, addresses, callback):
        hash2address = {
            bitcoin.address_to_scripthash(address): address
            for address in addresses}
        self.h2addr.update(hash2address)
        msgs = [
            ('blockchain.scripthash.subscribe', [x])
            for x in hash2address.keys()]
        self.send(msgs, self.map_scripthash_to_address(callback))

    def request_address_history(self, address, callback):
        h = bitcoin.address_to_scripthash(address)
        self.h2addr.update({h: address})
        self.send([('blockchain.scripthash.get_history', [h])], self.map_scripthash_to_address(callback))

    # NOTE this method handles exceptions and a special edge case, counter to
    # what the other ElectrumX methods do. This is unexpected.
    def broadcast_transaction(self, transaction, callback=None):
        command = 'blockchain.transaction.broadcast'
        invocation = lambda c: self.send([(command, [str(transaction)])], c)

        if callback:
            invocation(callback)
            return

        try:
            out = Network.__wait_for(invocation)
        except BaseException as e:
            print('broadcast_transaction failed 1', str(e))
            return False, "error: " + str(e)

        if out != transaction.txid():
            print('broadcast_transaction failed 2', str(transaction))
            return False, "error: " + out

        return True, out

    def get_history_for_scripthash(self, hash, callback=None):
        command = 'blockchain.scripthash.get_history'
        invocation = lambda c: self.send([(command, [hash])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)

    def subscribe_to_headers(self, callback=None):
        command = 'blockchain.headers.subscribe'
        invocation = lambda c: self.send([(command, [True])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)

    def subscribe_to_address(self, address, callback=None):
        command = 'blockchain.address.subscribe'
        invocation = lambda c: self.send([(command, [address])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)

    def get_merkle_for_transaction(self, tx_hash, tx_height, callback=None):
        command = 'blockchain.transaction.get_merkle'
        invocation = lambda c: self.send([(command, [tx_hash, tx_height])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)

    def subscribe_to_scripthash(self, scripthash, callback=None):
        command = 'blockchain.scripthash.subscribe'
        invocation = lambda c: self.send([(command, [scripthash])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)

    def get_transaction(self, transaction_hash, callback=None):
        command = 'blockchain.transaction.get'
        invocation = lambda c: self.send([(command, [transaction_hash])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)

    def get_transactions(self, transaction_hashes, callback=None):
        command = 'blockchain.transaction.get'
        messages = [(command, [tx_hash]) for tx_hash in transaction_hashes]
        invocation = lambda c: self.send(messages, c)

        return Network.__with_default_synchronous_callback(invocation, callback)

    def listunspent_for_scripthash(self, scripthash, callback=None):
        command = 'blockchain.scripthash.listunspent'
        invocation = lambda c: self.send([(command, [scripthash])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)

    def get_balance_for_scripthash(self, scripthash, callback=None):
        command = 'blockchain.scripthash.get_balance'
        invocation = lambda c: self.send([(command, [scripthash])], c)

        return Network.__with_default_synchronous_callback(invocation, callback)

    def get_transactions_receipt(self, tx_hashs, callback):
        command = 'blochchain.transaction.get_receipt'
        messages = [(command, [tx_hash]) for tx_hash in tx_hashs]
        invocation = lambda c: self.send(messages, c)
        return Network.__with_default_synchronous_callback(invocation, callback)

    def subscribe_tokens(self, tokens, callback):
        msgs = [(
            'blockchain.contract.event.subscribe',
            [bh2u(b58_address_to_hash160(token.bind_addr)[1]), token.contract_addr, TOKEN_TRANSFER_TOPIC])
            for token in tokens]
        self.send(msgs, callback)

    def get_token_info(self, contract_addr, callback=None):
        command = 'blockchain.token.get_info'
        invocation = lambda c: self.send([(command, [contract_addr, ])], c)
        return Network.__with_default_synchronous_callback(invocation, callback)

    def call_contract(self, address, data, sender, callback=None):
        command = 'blockchain.contract.call'
        invocation = lambda c: self.send([(command, [address, data, sender])], c)
        return Network.__with_default_synchronous_callback(invocation, callback)

    def request_token_balance(self, token, callback):
        """
        :type token: Token
        :param callback:
        :return:
        """
        __, hash160 = b58_address_to_hash160(token.bind_addr)
        hash160 = bh2u(hash160)
        datahex = '70a08231{}'.format(hash160.zfill(64))
        self.send([('blockchain.contract.call', [token.contract_addr, datahex, '', 'int'])],
                  callback)

    def request_token_history(self, token, callback):
        __, hash160 = b58_address_to_hash160(token.bind_addr)
        hash160 = bh2u(hash160)
        self.send([('blockchain.contract.event.get_history',
                    [hash160, token.contract_addr, TOKEN_TRANSFER_TOPIC])], callback)