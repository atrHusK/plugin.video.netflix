# -*- coding: utf-8 -*-
"""
    Copyright (C) 2017 Sebastian Golasch (plugin.video.netflix)
    Copyright (C) 2019 Stefano Gottardo - @CastagnaIT (original implementation module)
    Handle and build Netflix events

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
import queue
import random
import threading
import time
from typing import TYPE_CHECKING

import xbmc

from resources.lib.database.db_utils import TABLE_SESSION
from resources.lib.globals import G
from resources.lib.services.nfsession.msl import msl_utils
from resources.lib.services.nfsession.msl.msl_utils import (ENDPOINTS, EVENT_ENGAGE, create_req_params)
from resources.lib.utils.esn import get_esn
from resources.lib.utils.logging import LOG

if TYPE_CHECKING:  # This variable/imports are used only by the editor, so not at runtime
    from resources.lib.services.nfsession.nfsession_ops import NFSessionOperations


class EventsHandler(threading.Thread):
    """Handle and build Netflix event requests"""
    # This threaded class has been designed to handle a queue of HTTP requests in a sort of async way
    # this to avoid to freeze the 'xbmc.Monitor' notifications in the action_controller.py,
    # and also to avoid ugly delay in Kodi GUI when stop event occurs

    def __init__(self, chunked_request, nfsession: 'NFSessionOperations'):
        super().__init__()
        self.chunked_request = chunked_request
        self.nfsession = nfsession
        # session_id, app_id are common to all events
        self.session_id = int(time.time()) * 10000 + random.SystemRandom().randint(1, 10001)
        self.app_id = None
        self.queue_events = queue.Queue(maxsize=10)
        self.cache_data_events = {}
        self.banned_events_ids = []
        self._stop_requested = False

    def run(self):
        """Monitor and process the event queue"""
        LOG.debug('[Event queue monitor] Thread started')
        monitor = xbmc.Monitor()
        while not monitor.abortRequested() and not self._stop_requested:
            try:
                # Take the first queued item
                event_type, event_data, player_state = self.queue_events.get_nowait()
                # Process the request
                self._process_event_request(event_type, event_data, player_state)
            except queue.Empty:
                pass
            except Exception as exc:  # pylint: disable=broad-except
                LOG.error('[Event queue monitor] An error has occurred: {}', exc)
                import traceback
                LOG.error(traceback.format_exc())
                self.clear_queue()
            monitor.waitForAbort(0.5)

    def _process_event_request(self, event_type, event_data, player_state):
        """Build and make the event post request"""
        try:
            manifest = event_data['manifest']
            url = manifest['links']['events']['href']
            if not url:
                raise ValueError('manifest events link is empty')
            from resources.lib.services.nfsession.msl.msl_request_builder import MSLRequestBuilder
            event_params = self._build_event_params(event_type, event_data, player_state, manifest)
            request_data = MSLRequestBuilder.build_request_data(url, event_params)
            # Request attempts can be made up to a maximum of 3 times per event
            LOG.info('EVENT [{}] - Executing request (track context={}, ui context={})',
                     event_type, bool(event_params['trackId']),
                     'uiplaycontext' in event_params['sessionParams'])
            endpoint_url = ENDPOINTS['events'] + create_req_params(f'events/{event_type}')
            response = self.chunked_request(endpoint_url, request_data, get_esn())
            # Malformed/wrong content in requests are ignored without returning any error in the response or exception
            LOG.debug('EVENT [{}] - Request completed (response type={})', event_type,
                      type(response).__name__)
        except Exception as exc:  # pylint: disable=broad-except
            LOG.error('EVENT [{}] - The request has failed: {}', event_type, exc)
            # Ban future event requests from this event xid
            # self.banned_events_xid.append(request_data['xid'])
            # Todo: this has been disabled because unstable Wi-Fi connections may cause consecutive errors
            #   probably this should be handled in a different way

    def stop_join(self):
        self._stop_requested = True
        self.join()

    def add_event_to_queue(self, event_type, event_data, player_state):
        """Adds an event in the queue of events to be processed"""
        previous_data, _ = self.cache_data_events.get(event_data['videoid'].value, ({}, None))
        if previous_data.get('xid') in self.banned_events_ids:
            LOG.warn('EVENT [{}] - Not added to the queue. The xid {} is banned due to a previous failed request',
                     event_type, previous_data.get('xid'))
            return
        try:
            self.queue_events.put_nowait((event_type, event_data, player_state))
            LOG.debug('EVENT [{}] - Added to queue', event_type)
        except queue.Full:
            LOG.warn('EVENT [{}] - Not added to the queue. The event queue is full.', event_type)

    def clear_queue(self):
        """Clear all queued events"""
        with self.queue_events.mutex:
            self.queue_events.queue.clear()
        self.cache_data_events = {}
        self.banned_events_ids = []

    def _build_event_params(self, event_type, event_data, player_state, manifest):
        """Build data params for an event request"""
        videoid_value = event_data['videoid'].value
        # Get previous elaborated data of the same video id
        # Some tags must remain unchanged between events
        previous_data, previous_player_state = self.cache_data_events.get(videoid_value, ({}, None))
        timestamp = int(time.time() * 1000)

        position = max(0, int(player_state['current_pts'] * 1000))

        if msl_utils.is_media_changed(previous_player_state, player_state):
            play_times, video_track_id, audio_track_id, sub_track_id = msl_utils.build_media_tag(player_state, manifest,
                                                                                                 position)
        else:
            play_times = previous_data['playTimes']
            msl_utils.update_play_times_duration(play_times, player_state)
            video_track_id = previous_data['videoTrackId']
            audio_track_id = previous_data['audioTrackId']
            sub_track_id = previous_data['timedTextTrackId']

        tracking_id = event_data.get('tracking_id')
        if tracking_id in (None, 0, '0'):
            tracking_id = ''
        elif not isinstance(tracking_id, str):
            tracking_id = str(tracking_id)

        session_params = previous_data.get('sessionParams')
        if session_params is None:
            session_params = {'uiVersion': G.LOCAL_DB.get_value('ui_version', '', table=TABLE_SESSION)}
            ui_play_context = event_data.get('ui_play_context')
            if _is_complete_ui_play_context(ui_play_context, videoid_value):
                session_params['uiplaycontext'] = dict(ui_play_context)

        params = {
            'event': event_type,
            'xid': previous_data.get('xid', G.LOCAL_DB.get_value('xid', table=TABLE_SESSION)),
            'position': position,  # Video time elapsed
            'clientTime': timestamp,
            'sessionStartTime': previous_data.get('sessionStartTime', timestamp),
            'videoTrackId': video_track_id,
            'audioTrackId': audio_track_id,
            'timedTextTrackId': sub_track_id,
            'trackId': tracking_id,
            'sessionId': str(self.session_id),
            'appId': str(self.app_id or self.session_id),
            'playTimes': play_times,
            'sessionParams': session_params
        }

        # Cadmium omits these optional fields when playback state does not provide them.
        for optional_key in ('mediaId', 'programId'):
            if manifest.get(optional_key) is not None:
                params[optional_key] = manifest[optional_key]

        if event_type == EVENT_ENGAGE:
            params['action'] = 'User_Interaction'

        self.cache_data_events[videoid_value] = (params, player_state)
        return params


def _is_complete_ui_play_context(context, videoid_value):
    """Return whether the context contains real UI values instead of synthetic placeholders."""
    if not isinstance(context, dict):
        return False
    required_values = ('lolomo_id', 'location', 'request_id', 'track_id', 'video_id')
    if any(context.get(key) in (None, '', 0, '0', 'unknown') for key in required_values):
        return False
    if str(context['video_id']) != str(videoid_value):
        return False
    return isinstance(context.get('rank'), int) and isinstance(context.get('row'), int)
