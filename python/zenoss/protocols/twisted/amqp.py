##############################################################################
#
# Copyright (C) Zenoss, Inc. 2010, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

import logging
import zlib
from os.path import join as pathjoin, dirname
from twisted.internet import reactor
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.internet import defer
from twisted.internet.defer import inlineCallbacks, returnValue, Deferred, DeferredList
from twisted.python.failure import Failure
from txamqp import spec
from txamqp.queue import Closed as QueueClosed
from txamqp.protocol import AMQClient
from txamqp.client import TwistedDelegate, Closed
from txamqp.content import Content
from zope.component import getAdapter
from zenoss.protocols.interfaces import IAMQPChannelAdapter
from zenoss.protocols.exceptions import ChannelClosedError

from zenoss.protocols.amqp import set_keepalive

log = logging.getLogger('zen.protocols.twisted')


class AMQProtocol(AMQClient):
    """
    Protocol for an AMQ connection. Has methods for sending to an exchange and
    listening to a queue.

    Protocols get created every time a connection is made, so we can't store
    anything on here that needs to outlive a given connection; we'll store
    things on the factory and ask it for that information.
    """
    _connected = False
    _counter = 2

    @inlineCallbacks
    def connectionMade(self):
        """
        Hook called when the connection is made; we'll use this to perform
        exchange setup, etc.
        """
        try:
            connectionInfo = self.factory.connectionInfo
            set_keepalive(self.transport.socket,
                          connectionInfo.amqpconnectionheartbeat)
            AMQClient.connectionMade(self)
            log.debug('Made initial connection to message broker')
            self._connected = False
            # Authenticate
            try:
                yield self.start({'LOGIN': connectionInfo.user,
                                  'PASSWORD': connectionInfo.password})
                self.factory.onAuthenticated(True)
                log.debug('Successfully authenticated as %s',
                          connectionInfo.user)
            except Closed as err:
                log.debug("Error authenticating to %s as %s",
                          (connectionInfo.host, connectionInfo.user))
                # If authentication fails, errback the onAuthentication trigger
                # to the reactor, and return a Failure for this deferred
                self.factory.onAuthentication("authentication failed")
                returnValue(Failure())

            # Get a channel
            self.chan = yield self.get_channel()
            self._connected = True

            # Set prefetch limit if necessary
            if getattr(self, "prefetch", None) and not getattr(self.chan, '_flag_qos', False):
                self.chan.basic_qos(prefetch_count=self.prefetch)
                self.chan._flag_qos = True

            # Initialize the queues
            yield self.begin_listening()

            # Call back any onConnectionMade deferreds registered by the factory
            self.factory.onConnectionMade(self)

            # Flush any messages that have been sent before now
            yield self.send()
            returnValue("success")
        except Exception as err:
            log.exception("Unable to connect")
            self.factory.onConnectionFailed("protocol connectionMade failed")
            returnValue(Failure())

    def is_connected(self):
        return self._connected

    @inlineCallbacks
    def get_channel(self):
        """
        Get a channel.
        """
        try:
            log.debug("begin: AMQProtocol.get_channel()")
            chan = yield self.channel(self._counter)
            self._counter += 1
            yield chan.channel_open()
            log.debug('Channel opened')
            returnValue(chan)
        except Exception as err:
            log.exception(err)
            returnValue(Failure(err))

    @inlineCallbacks
    def listen_to_queue(self, queue, callback, exclusive=False):
        """
        Get a queue and register a callback to be executed when a message is
        received, then begin listening.
        """
        if self.is_connected():
            twisted_queue = yield self.get_queue(queue, exclusive)
            log.debug('Listening to queue %s' % queue.name)
            # Start the recursive call to listen for messages. Not yielding on this because
            # we don't want to block while waiting for the first message.
            self.processMessages(twisted_queue, callback)

    @inlineCallbacks
    def begin_listening(self):
        """
        Iterate over all queues registered in the factory and start listening
        to them.
        """
        log.debug('Binding to %s queues' % len(self.factory.queues))
        for queue, cb, exclusive in self.factory.queues:
            yield self.listen_to_queue(queue, cb, exclusive)

    @inlineCallbacks
    def get_queue(self, queue, exclusive=False):
        """
        Perform all the setup to get a queue, then return it.
        """
        yield self.create_queue(queue)

        # Start consuming from the queue (this actually creates it)
        result = yield self.chan.basic_consume(queue=queue.name, exclusive=exclusive)

        # Go get the queue and return it
        queue = yield self.queue(result[0]) # result[0] contains the consumer tag
        returnValue(queue)

    @inlineCallbacks
    def create_queue(self, queue):
        # Declare the queue
        try:
            yield getAdapter(self.chan, IAMQPChannelAdapter).declareQueue(queue)
        except ChannelClosedError as err:
            # Here we handle the case where we redeclare a queue
            # with different properties. When this happens, Rabbit
            # both returns an error and closes the channel. We
            # need to detect this and reopen the channel, since
            # the existing queue will work fine (although it will
            # not use the modified config).
            if e.replyCode == 406:
                # PRECONDITION_FAILED -- properties changed
                # Remove the channel and allow it to be reopened
                log.warn(("Attempted to redeclare queue {0} with "
                          "different arguments. You will need to "
                          "delete the queue to pick up the new "
                          "configuration.").format(queue.name))
                log.debug(err)
                self.chan = yield self.get_channel()
            else:
                raise

    @inlineCallbacks
    def send_message(self, exchange, routing_key, msg, mandatory=False,
                     headers=None, declareExchange=True):
        body = msg
        headers = headers if headers else {}

        # it is a protobuf
        if not isinstance(msg, basestring):
            body = msg.SerializeToString()
            headers['X-Protobuf-FullName'] = msg.DESCRIPTOR.full_name

        queueSchema = self.factory.queueSchema
        exchangeConfig = queueSchema.getExchange(exchange)

        if declareExchange:
            # Declare the exchange to which the message is being sent
            yield getAdapter(self.chan, IAMQPChannelAdapter).declareExchange(exchangeConfig)

        if exchangeConfig.compression == 'deflate':
            body = zlib.compress(body)

        content = Content(body)
        content.properties['delivery-mode'] = exchangeConfig.delivery_mode
        content.properties['headers'] = headers
        content.properties['content-type'] = 'application/x-protobuf'

        if exchangeConfig.compression == 'deflate':
            content.properties['content-encoding'] = 'deflate'

        # Publish away
        yield self.chan.basic_publish(exchange=exchangeConfig.name,
                                      routing_key=routing_key,
                                      content=content,
                                      mandatory=mandatory)
        returnValue("SUCCESS")

    def send(self):
        """
        Send any messages queued on the factory.
        """
        if self.is_connected():
            dList = []
            while self.factory.messages:
                message_args = self.factory.messages.pop()
                dList.append(self.send_message(*message_args))
            d = DeferredList(dList)
            d.addCallback(self.factory.onInitialSend)
            return d
        return self.factory._onInitialSend

    def acknowledge(self, message, multiple=False):
        """
        Acknowledges a message
        """
        return self.chan.basic_ack(delivery_tag=message.delivery_tag, multiple=multiple)

    def reject(self, message, requeue=False):
        """
        Rejects a message and optionally requeues it.
        """
        return self.chan.basic_reject(delivery_tag=message.delivery_tag, requeue=requeue)

    @inlineCallbacks
    def processMessages(self, queue, callback):
        """
        Gets messages from a queue and fires callbacks, then calls itself to
        continue listening.
        """
        try:
            message = yield queue.get()
        except QueueClosed:
            log.info('Connection to queue closed')
            self.factory.disconnect()
        except Exception:
            log.exception("Exception while getting message from queue.")
            raise
        else:
            reactor.callLater(0, self._doCallback, queue, callback, message)
            returnValue(None)

    @inlineCallbacks
    def _doCallback(self, queue, callback, message):
        if message.content.properties.get('content-encoding', None) == 'deflate':
            try:
                message.content.body = zlib.decompress(message.content.body)
            except zlib.error as e:
                log.exception("Unable to decode event.")

        yield defer.maybeDeferred(callback, message) # synchronous
        self.processMessages(queue, callback)

    def connectionLost(self, reason):
        log.debug("connection lost %s" % reason)
        AMQClient.connectionLost(self, reason)
        self.factory.onConnectionLost(reason)


class AMQPFactory(ReconnectingClientFactory):
    """
    The actual service. This is what should be used to listen to queues and
    send messages; these are buffered on the factory, then referenced by the
    protocol each time a connection is made.

    provides asynchronous hooks, which you can yield to
    in a deferred.inlineCallbacks method,
    to wait for the specified event to occur before proceeding.
    """
    protocol = AMQProtocol

    def __init__(self, amqpConnectionInfo, queueSchema, reactor=reactor):
        with open(pathjoin(dirname(__file__), 'amqp0-9-1.xml')) as f:
            self.spec = spec.load(f)
            log.debug('Loaded AMQP spec')
        self.connectionInfo = amqpConnectionInfo
        self.queueSchema = queueSchema
        self.vhost = self.connectionInfo.vhost
        self.host = self.connectionInfo.host
        self.port = self.connectionInfo.port
        self.usessl = self.connectionInfo.usessl
        self.delegate = TwistedDelegate()
        self.queues = []
        self.messages = []
        self.p = None
        # Event Hooks. yield to these deferreds to wait for the event
        self._onConnectionMade = self._createDeferred()
        self._onConnectionLost = self._createDeferred()
        self._onConnectionFailed = self._createDeferred()
        self._onAuthenticated = self._createDeferred()
        self._onInitialSend = self._createDeferred()

        self.reactor = reactor
        self.connector = self.reactor.connectTCP(self.host, self.port, self)
        self.heartbeat = self.connectionInfo.amqpconnectionheartbeat
        self.prefetch = None

    def _defaultErrback(self, failure):
        log.error('Error: %s', failure)
        print('Error: {}'.format(failure))
        print('Error.value: {}'.format(failure.value))
        #print('Error.stack: {}'.format(failure.stack))
        return failure

    def _createDeferred(self):
        d = Deferred()
        return d.addErrback(self._defaultErrback)

    def onAuthenticated(self, din):
        log.debug('AMQPFactory.onAuthenticated(%s)', din)
        d, self._onAuthenticated = self._onAuthenticated, defer.Deferred()
        #d = defer.Deferred()
        #d.addCallback(self._onAuthenticated)
        #d.addErrback(self._defaultErrback)
        d.callback(din)
        return d

    '''def _onAuthenticated(self, din):
        log.debug('AMQPFactory._onAuthenticated(%s)', str(din))
        return din
    '''

    def onConnectionMade(self, value):
        '''Register a deferred that AMQProtocol's onConnectionMade method
        will callback when it completes.
        '''
        set_keepalive(self.connector.transport.socket, self.heartbeat)
        d, self._onConnectionMade = self._onConnectionMade, self._createDeferred()
        d.callback(value)
        return d

    def onConnectionLost(self, reason):
        '''Trigger the _onConnectionLost Hook
        you can add clientConnectionLost as a callback
        to the _onConnectionLost deferred, to trigger a reconnection attempt
        '''
        log.debug('onConnectionLost %s' % reason)
        d, self._onConnectionLost = self._onConnectionLost, self._createDeferred()
        #d.addCallback(self.clientConnectionLost)
        #d.callback(self.connector, reason)
        d.callback(reason)
        return d

    def onConnectionFailed(self, msg):
        '''Trigger the _onConnectionFailed Hook
        applications may subscribe to this hook
        by yielding to it in an inline callback
        which will wait for the hook to be triggered and return some value
        before continuing
        '''
        log.debug('onConnectionFailed %s' % msg)
        d, self._onConnectionFailed = self._onConnectionFailed, self._createDeferred()
        #d = Deferred()
        #d.addCallback(self.clientConnectionFailed)
        #d.addErrback(self._defaultErrback)
        #d.callback((self.connector, din))
        d.callback(msg)
        return d

    def onInitialSend(self, value):
        d, self._onInitialSend = self._onInitialSend, self._createDeferred()
        d.callback(value)
        return d

    def buildProtocol(self, addr, clock=reactor):
        self.p = self.protocol(self.delegate,
                               self.vhost,
                               self.spec,
                               self.heartbeat,
                               clock=clock)
        self.p.factory = self
        self.p.prefetch = self.prefetch
        self.resetDelay()
        return self.p

    def setPrefetch(self, prefetch):
        self.prefetch = prefetch

    def listen(self, queue, callback, exclusive=False):
        """
        Listen to a queue.

        @param queue: The queue to listen to.
        @type queue: zenoss.protocols.queueschema.Queue
        @param callback: The function to be called when a message is received
        in this queue.
        @type callback: callable
        """
        args = queue, callback, exclusive
        self.queues.append(args)
        if self.p is not None:
            self.p.listen_to_queue(*args)

    def send(self, exchangeIdentifier, routing_key, message, mandatory=False, headers=None,
             declareExchange=True):
        """
        Send a C{message} to exchange C{exchange}.

        Appends to the factory message buffer, then tells the protocol to read
        from that buffer.

        @param exchangeIdentifier: The exchange to send to
        @type exchangeIdentifier: str
        @param routing_key: The routing_key for the message
        @type routing_key: str
        @param message: The message to send
        @type message: str
        @param mandatory: Whether the mandatory bit should be set.
        @type mandatory: bool
        @param headers: The message headers used when publishing the message.
        @type headers: dict
        """
        self.messages.append((exchangeIdentifier, routing_key, message, mandatory, headers, declareExchange))
        if self.p is not None:
            return self.p.send()
        else:
            return self._onInitialSend


    def acknowledge(self, message):
        """
        Acknowledges a message so it is removed from the queue
        """
        return self.p.acknowledge(message)

    def reject(self, message, requeue=False):
        """
        Rejects a message and optionally requeues it.
        """
        return self.p.reject(message, requeue=requeue)

    def disconnect(self):
        log.info("Disconnecting AMQP Client.")
        self.connector.disconnect()
        self.p = None

    def shutdown(self):
        """
        Disconnect completely and call back our connectionLost Deferred.
        """
        isConnected = (self.connector.state == 'connected')
        self.connector.disconnect()
        self.stopTrying()

        # If we are connected, then wait for connection to be closed.
        if isConnected:
            shutdownDeferred = Deferred()
            def connectionShut(result):
                log.debug("Connection Shut: %s", result)
                shutdownDeferred.callback("connection shut down")
            self._onConnectionLost.addBoth(connectionShut)
            return shutdownDeferred

        return defer.succeed(None)

    @property
    def channel(self):
        return self.p.chan

    def createQueue(self, queueIdentifier, replacements=None):
        '''if this factory has a protocol,
        run the protocol's create_queue method
        else add it as a callback on the _onConnectionMade deferred.
        '''
        log.debug("begin: AMQPFactory.createQueue(%s, replacements=%s)",
                  queueIdentifier, replacements)
        queue = self.queueSchema.getQueue(queueIdentifier, replacements)
        if self.p is not None:
            return self.p.create_queue(queue)
        else:
            def doCreateQueue(value):
                return self.p.create_queue(queue)

            d = self.onConnectionMade('start', delay=True)
            d.addCallback(doCreateQueue)
            d.addErrback(self._defaultErrback)
            return d

    def clientConnectionFailed(self, connector, reason):
        log.debug('Client connection failed: %s', reason)
        #self.onConnectionFailed(reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)

    def clientConnectionLost(self, connector, reason):
        log.debug('Client connection lost: %s', reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)
