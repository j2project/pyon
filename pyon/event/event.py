#!/usr/bin/env python

"""Events and Notifications"""

__author__ = 'Dave Foster <dfoster@asascience.com>, Michael Meisinger'
__license__ = 'Apache 2.0'

import time

from pyon.core import bootstrap
from pyon.core.exception import BadRequest, IonException
from pyon.datastore.datastore import DataStore
from pyon.net.endpoint import Publisher, Subscriber
from pyon.util.containers import get_ion_ts
from pyon.util.log import log

from interface.objects import Event

# @TODO: configurable
EVENTS_XP = "pyon.events"
EVENTS_XP_TYPE = "topic"

def get_events_exchange_point():
    return "%s.%s" % (bootstrap.get_sys_name(), EVENTS_XP)


class EventError(IonException):
    status_code = 500

class EventPublisher(Publisher):

    def __init__(self, event_type, xp=None, **kwargs):
        """
        Constructs a publisher of events for a specific type.

        @param  event_type  The name of the event type object
        @param  xp          Exchange (AMQP) name, can be none, will use events default.
\       """

        self.event_type = event_type

        if bootstrap.container_instance and getattr(bootstrap.container_instance, 'event_repository', None):
            self.event_repo = bootstrap.container_instance.event_repository
        else:
            self.event_repo = None

        # generate an exchange name to publish events to
        xp = xp or get_events_exchange_point()
        name = (xp, None)

        Publisher.__init__(self, to_name=name, **kwargs)

    def _topic(self, origin):
        """
        Builds the topic that this event should be published to.
        """
        assert self.event_type and origin
        return "%s.%s" % (str(self.event_type), str(origin))

    def _create_event(self, **kwargs):
        assert self.event_type

        if 'ts_created' not in kwargs:
            kwargs['ts_created'] = get_ion_ts()

        event_msg = bootstrap.IonObject(self.event_type, **kwargs)
        event_msg.base_types = event_msg._get_extends()
        # Would like to validate here but blows up if an object is provided where a dict is
        #event_msg._validate()

        return event_msg

    def _publish_event(self, event_msg, origin, **kwargs):
        assert origin

        to_name = (self._send_name.exchange, self._topic(origin))
        log.debug("Publishing event message to %s", to_name)

        ep = self.publish(event_msg, to_name=to_name)
        ep.close()

        # store published event but only if we specified an event_repo
        if self.event_repo:
            self.event_repo.put_event(event_msg)

    def publish_event(self, origin=None, **kwargs):
        event_msg = self._create_event(origin=origin, **kwargs)
        self._publish_event(event_msg, origin=origin)


class EventSubscriber(Subscriber):

    def _topic(self, origin):
        """
        Builds the topic that this event should be published to.
        If either side of the event_id.origin pair are missing, will subscribe to anything.
        """
        event_type  = self.event_type or "*"
        origin      = origin or "#"

        return "%s.%s" % (event_type, origin)

    def __init__(self, xp_name=None, event_type=None, origin=None, queue_name=None, callback=None, *args, **kwargs):
        """
        Initializer.

        If the queue_name is specified here, the sysname is prefixed automatically to it. This is becuase
        named queues are not namespaces to their exchanges, so two different systems on the same broker
        can cross-pollute messages if a named queue is used.
        """
        self.event_type = event_type

        xp_name = xp_name or get_events_exchange_point()
        binding = self._topic(origin)

        # prefix the queue_name, if specified, with the sysname
        # this is because queue names transcend xp boundaries (see R1 OOIION-477)
        if queue_name is not None:
            if not queue_name.startswith(bootstrap.get_sys_name()):
                queue_name = "%s.%s" % (bootstrap.get_sys_name(), queue_name)
                log.warn("queue_name specified, prepending sys_name to it: %s" % queue_name)

        name = (xp_name, queue_name)

        Subscriber.__init__(self, from_name=name, binding=binding, callback=callback, **kwargs)


class EventRepository(object):
    """
    Class that uses a data store to provide a persistent repository for ION events.
    """

    def __init__(self, datastore_manager=None):

        # Get an instance of datastore configured as directory.
        # May be persistent or mock, forced clean, with indexes
        datastore_manager = datastore_manager or bootstrap.container_instance.datastore_manager
        self.event_store = datastore_manager.get_datastore("events", DataStore.DS_PROFILE.EVENTS)

    def close(self):
        """
        Pass-through method to close the underlying datastore.
        """
        self.event_store.close()

    def put_event(self, event):
        log.debug("Store event persistently %s" % event)
        if not isinstance(event, Event):
            raise BadRequest("event must be type Event, not %s" % type(event))
        return self.event_store.create(event)

    def get_event(self, event_id):
        log.debug("Retrieving persistent event for id=%s" % event_id)
        event_obj = self.event_store.read(event_id)
        return event_obj

    def find_events(self, event_type=None, origin=None, start_ts=None, end_ts=None, **kwargs):
        log.debug("Retrieving persistent event for event_type=%s, origin=%s, start_ts=%s, end_ts=%s, descending=%s, limit=%s" % (
                event_type,origin,start_ts,end_ts,kwargs.get("descending", None),kwargs.get("limit",None)))
        events = None

        design_name = "event"
        view_name = None
        start_key = []
        end_key = []
        if origin and event_type:
            view_name = "by_origintype"
            start_key=[origin, event_type]
            end_key=[origin, event_type]
        elif origin:
            view_name = "by_origin"
            start_key=[origin]
            end_key=[origin]
        elif event_type:
            view_name = "by_type"
            start_key=[event_type]
            end_key=[event_type]
        elif start_ts or end_ts:
            view_name = "by_time"
            start_key=[]
            end_key=[]
        else:
            view_name = "by_time"
            if kwargs.get("limit", 0) < 1:
                kwargs["limit"] = 100
                log.warn("Querying all events, no limit given. Set limit to 100")

        if start_ts:
            start_key.append(start_ts)
        if end_ts:
            end_key.append(end_ts)

        events = self.event_store.find_by_view(design_name, view_name, start_key=start_key, end_key=end_key,
                                                id_only=False, **kwargs)

        return events
