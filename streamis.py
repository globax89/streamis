"""Streamis - Subscribe to Redis pubsub channels via HTTP and EventSource."""

import logging

import asyncio
from asyncio import Queue
import aioredis

from tornado.platform.asyncio import AsyncIOMainLoop
from tornado import web
from tornado.iostream import StreamClosedError
from tornado.options import options, define

AsyncIOMainLoop().install()

logger = logging.getLogger('streamis')

define('redis-host', default='localhost', help='Redis server hostname')
define('redis-port', default=6379, help='Redis server port')
define('port', default=8989, help='HTTP port to serve on')
define('debug', default=False, help='Enable debug mode')


class Connection:
    _redis = None

    @classmethod
    async def redis(cls, force_reconnect=False):
        if cls._redis is None or force_reconnect:
            settings = (options.redis_host, options.redis_port)
            cls._redis = await aioredis.create_redis(settings)
        return cls._redis


class Subscription:
    """Handles subscriptions to Redis PUB/SUB channels."""
    def __init__(self, redis, channel: str):
        self._redis = redis
        self.name = channel
        self.listeners = set()

    async def subscribe(self):
        res = await self._redis.subscribe(self.name)
        self.channel = res[0]

    def __str__(self):
        return self.name

    def add_listener(self, listener):
        self.listeners.add(listener)

    async def broadcast(self):
        """Listen for new messages on Redis and broadcast to all
        HTTP listeners.

        """
        while len(self.listeners) > 0:
            msg = await self.channel.get()
            logger.debug("Got message: %s" % msg)
            closed = []
            for listener in self.listeners:
                try:
                    listener.queue.put_nowait(msg)
                except:
                    logger.warning('Message delivery failed. Client disconnection?')
                    closed.append(listener)
            if len(closed) > 0:
                [self.listeners.remove(listener) for listener in closed]


class SubscriptionManager:
    """Manages all subscriptions."""
    def __init__(self, loop=None):
        self.redis = None
        self.subscriptions = dict()
        self.loop = loop or asyncio.get_event_loop()

    async def connect(self):
        self.redis = await Connection.redis()

    async def subscribe(self, listener, channel: str):
        """Subscribe to a new channel."""
        if channel in self.subscriptions:
            subscription = self.subscriptions[channel]
        else:
            subscription = Subscription(self.redis, channel)
            await subscription.subscribe()
            self.subscriptions[channel] = subscription
            self.loop.call_soon(lambda: asyncio.Task(subscription.broadcast()))
        subscription.add_listener(listener)

    def unsubscribe(self, channel: str):
        """Unsubscribe from a channel."""
        if channel not in self.subscriptions:
            logger.warning("Not subscribed to channel '%s'" % channel)
            return
        sub = self.subscriptions.pop(channel)
        del sub


class SSEHandler(web.RequestHandler):
    def initialize(self, manager: SubscriptionManager):
        self.queue = Queue()
        self.manager = manager
        self.set_header('content-type', 'text/event-stream')
        self.set_header('cache-control', 'no-cache')

    async def get(self, channel: str):
        await self.manager.subscribe(self, channel)
        while True:
            message = await self.queue.get()
            try:
                self.write("data: %s\n\n" % message)
                await self.flush()
            except StreamClosedError:
                break


def main():
    options.parse_command_line()
    loop = asyncio.get_event_loop()

    manager = SubscriptionManager()
    loop.run_until_complete(manager.connect())

    app = web.Application(
        [(r'/(.*)', SSEHandler, dict(manager=manager))],
        debug=options.debug
    )
    app.listen(options.port)
    logger.info('Listening on port %d' % options.port)
    loop.run_forever()


if __name__ == "__main__":
    main()
