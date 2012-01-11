 #!/usr/bin/env python

"""AMQP messaging with Pika."""

import gevent
from gevent import event, coros

from pika.credentials import PlainCredentials
from pika.connection import ConnectionParameters
from pika.adapters import SelectConnection
from pika import BasicProperties

from pyon.core.bootstrap import CFG
from pyon.net import amqp
from pyon.net import channel
from pyon.net.channel import BaseChannel
from pyon.util.async import blocking_cb
from pyon.util.log import log
from pyon.util.pool import IDPool

class NodeB(amqp.Node):
    """
    Blocking interface to AMQP messaging primitives.

    Wrap around Node and create blocking interface for getting channel
    objects.
    """

    def __init__(self):
        log.debug("In NodeB.__init__")
        self.ready = event.Event()
        self._lock = coros.RLock()
        self._pool = IDPool()
        self._bidir_pool = {}   # maps inactive/active our numbers (from self._pool) to channels
        self._pool_map = {}     # maps active pika channel numbers to our numbers (from self._pool)

        amqp.Node.__init__(self)

    def start_node(self):
        """
        This should only be called by on_connection_opened.
        so, maybe we don't need a start_node/stop_node interface
        """
        log.debug("In start_node")
        amqp.Node.start_node(self)
        self.running = 1
        self.ready.set()

    def channel(self, ch_type, channel_create_callback):
        """
        Creates a Channel object with an underlying transport callback and returns it.

        @type ch_type   BaseChannel
        """
        log.debug("NodeB.channel")
        with self._lock:
            def new_channel():
                chan = channel_create_callback(close_callback=self.on_channel_request_close)
                amq_chan = blocking_cb(self.client.channel, 'on_open_callback')
                chan.on_channel_open(amq_chan)

                return chan

            # having _queue_auto_delete on is a pre-req to being able to pool.
            if ch_type == channel.BidirClientChannel and not ch_type._queue_auto_delete:
                chid = self._pool.get_id()
                if chid in self._bidir_pool:
                    log.debug("BidirClientChannel requested, pulling from pool (%d)", chid)
                    assert not chid in self._pool_map.values()
                    ch = self._bidir_pool[chid]
                    self._pool_map[ch.get_channel_id()] = chid
                else:
                    log.debug("BidirClientChannel requested, no pool items available, creating new (%d)", chid)
                    ch = new_channel()
                    self._bidir_pool[chid] = ch
                    self._pool_map[ch.get_channel_id()] = chid
            else:
                ch = new_channel()
            assert ch

        return ch

    def on_channel_request_close(self, ch):
        log.debug("NodeB: on_channel_request_close\n\tChType %s, Ch#: %d", ch.__class__, ch.get_channel_id())

        if ch.get_channel_id() in self._pool_map:
            with self._lock:
                ch.stop_consume()
                chid = self._pool_map.pop(ch._amq_chan.channel_number)
                log.debug("Releasing BiDir pool Pika #%d, our id #%d", ch.get_channel_id(), chid)
                self._pool.release_id(chid)

                # sanity check: if auto delete got turned on, we must remove this channel from the pool
                if ch._queue_auto_delete:
                    log.warn("A pooled channel now has _queue_auto_delete set true, we must remove it: check what caused this as it's likely a timing error")

                    self._bidir_pool.pop(chid)
                    self._pool._ids_free.remove(chid)

        else:
            ch.close_impl()

def ioloop(connection):
    # Loop until CTRL-C
    log.debug("In ioloop")
    import threading
    threading.current_thread().name = "NODE"
    try:
        # Start our blocking loop
        log.debug("Before start")
        connection.ioloop.start()
        log.debug("After start")

    except KeyboardInterrupt:

        log.debug("Got keyboard interrupt")

        # Close the connection
        connection.close()

        # Loop until the connection is closed
        connection.ioloop.start()

def make_node(connection_params=None):
    """
    Blocking construction and connection of node.

    @param connection_params  AMQP connection parameters. By default, uses CFG.server.amqp (most common use).
    """
    log.debug("In make_node")
    node = NodeB()
    connection_params = connection_params or CFG.server.amqp
    credentials = PlainCredentials(connection_params["username"], connection_params["password"])
    conn_parameters = ConnectionParameters(host=connection_params["host"], virtual_host=connection_params["vhost"], port=connection_params["port"], credentials=credentials)
    connection = SelectConnection(conn_parameters , node.on_connection_open)
    ioloop_process = gevent.spawn(ioloop, connection)
    #ioloop_process = gevent.spawn(connection.ioloop.start)
    node.ready.wait()
    return node, ioloop_process
    #return node, ioloop, connection

def testb():
    log.debug("In testb")
    node, ioloop_process = make_node()
    ch = node.channel(channel.BaseChannel)
    print ch
    ch.bind(('amq.direct', 'foo'))
    print 'bound'
    ch.listen(1)
    print 'listening'
    msg = ch.recv()
    print 'message: ', msg
    ioloop_process.join()

def testbclient():
    log.debug("In testbclient")
    node, ioloop_process = make_node()
    ch = node.channel(channel.BidirectionalClient)
    print ch
    ch.connect(('amq.direct', 'foo'))
    print 'sending'
    ch.send('hey, whats up?')
    print 'sent'
    print 'receiving'
    msg = ch.recv()
    print 'message: ', msg

def test_accept():
    log.debug("In test_accept")
    node, ioloop_process = make_node()
    ch = node.channel(channel.Bidirectional)
    print ch
    ch.bind(('amq.direct', 'foo'))
    print 'bound'
    ch.listen(1)
    ch_serv = ch.accept() # do we need the name of the connected peer?
    print 'accepted'
    msg = ch_serv.recv()
    print 'message: ', msg
    ch_serv.send('not much, dude')

    ioloop_process.join()

class NodeNB(amqp.Node):
    """
    Main non blocking messaging interface that goes active when amqp client connects.
    Integrates messaging and processing

    The life cycle of this depends on the underlying amqp connection.

    This thing (or a subordinate but coupled/controlled object) mediates
    Messaging Channels that are used to send messages or that dispatch
    received messages to receiver/consumer protocol things.
    """

    def __init__(self):
        log.debug("In NodeNB.__init__")
        self.channels = {}
        self.id_pool = IDPool()

    def start_node(self):
        """
        This should only be called by on_connection_opened..
        so, maybe we don't need a start_node/stop_node interface
        """
        log.debug("In NodeNB.start_node")
        amqp.Node.start_node(self)
        for ch_id in self.channels:
            self.start_channel(ch_id)

    def stop_node(self):
        """
        """
        log.debug("In NodeNB.stop_node")

    def channel(self, ch_type):
        """
        ch_type is one of PointToPoint, etc.
        return Channel instance that will be activated with amqp_channel
        and configured

        name (exchange, key)
        name shouldn't need to be specified here, but it is for now
        """
        log.debug("In NodeNB.channel")
        ch = ch_type()
        ch_id = self.id_pool.get_id()
        log.debug("channel id: %s" % str(ch_id))
        self.channels[ch_id] = ch
        if self.running:
            self.start_channel(ch_id)
        log.debug("channel: %s" % str(ch))
        return ch

    def start_channel(self, ch_id):
        log.debug("In NodeNB.start_channel")
        log.debug("ch_id: %s" % str(ch_id))
        ch = self.channels[ch_id]
        log.debug("ch: %s" % str(ch))
        self.client.channel(ch.on_channel_open)

    def spawnServer(self, f):
        """
        """
        log.debug("In spawnServer")

class TestServer(channel.BaseChannel):

    def do_config(self):
        log.debug("In TestServer.do_config")
        self._chan_name = ('amq.direct', 'foo')
        self.queue = 'foo'
        self.do_queue()

class TestClient(channel.BaseChannel):

    def do_config(self):
        log.debug("In TestClient.do_config")
        self._peer_name = ('amq.direct', 'foo')
        self.send('test message')


def testnb():
    log.debug("In testnb")
    node = NodeNB()
    #ch = node.channel(('amq.direct', 'foo'), TestServer)
    ch = node.channel(TestServer)
    #ch = node.channel(TestClient)
    conn_parameters = ConnectionParameters()
    connection = SelectConnection(conn_parameters , node.on_connection_open)
    # Loop until CTRL-C
    try:
        # Start our blocking loop
        connection.ioloop.start()

    except KeyboardInterrupt:

        # Close the connection
        connection.close()

        # Loop until the connection is closed
        connection.ioloop.start()


if __name__ == '__main__':
    #testnb()
    #testb()
    testbclient()
    #test_accept()
