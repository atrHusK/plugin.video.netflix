# -*- coding: utf-8 -*-
"""
    Copyright (C) 2017 Sebastian Golasch (plugin.video.netflix)
    Copyright (C) 2020 Stefano Gottardo (original implementation module)
    Builds and executes PATH requests for the directories

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
from typing import TYPE_CHECKING
from collections import OrderedDict
from types import SimpleNamespace
import json
import re
import time
import uuid
from urllib.parse import urlencode

import requests.exceptions as req_exceptions

from resources.lib import common
from resources.lib.utils.data_types import (VideoListSorted, SubgenreList, SeasonList, EpisodeList, LoCo, VideoList,
                                            SearchVideoList, CustomVideoList, LoLoMoCategory, VideoListSupplemental,
                                            VideosList)
from resources.lib.common.exceptions import InvalidVideoListTypeError, InvalidVideoId, MetadataNotAvailable
from resources.lib.database.db_utils import TABLE_SESSION
from resources.lib.utils.api_paths import (VIDEO_LIST_PARTIAL_PATHS, RANGE_PLACEHOLDER, VIDEO_LIST_BASIC_PARTIAL_PATHS,
                                           SEASONS_PARTIAL_PATHS, EPISODES_PARTIAL_PATHS, ART_PARTIAL_PATHS, ART_SIZE_FHD, ART_SIZE_POSTER, ART_SIZE_SD,
                                           TRAILER_PARTIAL_PATHS, PATH_REQUEST_SIZE_STD, build_paths,
                                           PATH_REQUEST_SIZE_MAX)
from resources.lib.common import cache_utils
from resources.lib.globals import G
from resources.lib.utils.logging import LOG

GRAPHQL_URL = 'https://web.prod.cloud.netflix.com/graphql'
GRAPHQL_OP_SEASONS = 'dbc3b274-d4f9-4811-aaf1-d082d3b936f2'
GRAPHQL_OP_EPISODES = '27b30e4e-871d-46aa-ac8b-244103d2e37d'
GRAPHQL_OP_SEARCH = '8d902979-56f2-4886-8c16-f8910f6b52ee'
LOCO_ROOT_ID_RE = re.compile(r'NES_[A-Za-z0-9_]+_p_\d+')
LOCO_ROW_RANGE = {'from': 0, 'to': 50}
LOCO_PAGE_RANGE = {'from': 0, 'to': 20}
LOCO_REFERENCE_FIELDS = [
    'availability', 'episodeCount', 'inRemindMeList', 'queue', 'summary',
    'title', 'synopsis', 'runtime', 'seasonCount', 'bookmarkPosition',
    'creditsOffset', 'watched', 'delivery', 'trackIds', 'userRating',
    'maturity', 'releaseYear'
]
LOCO_CATEGORY_CONTEXTS = {
    'comingSoon': ('newThisWeek', 'popularTitles', 'mostWatched', 'trendingNow'),
    'recommendations': ('similars', 'becauseYouAdded', 'becauseYouLiked', 'watchAgain', 'bigRow',
                        'topTen', 'trendingNow', 'popularTitles')
}
SORTED_LIST_CONTEXT_FALLBACKS = {
    ('genres', '1592210'): 'newThisWeek'
}
BROWSER_LOCO_ROW_KEYS = [0, 1, 2, 3, 'continueWatching']
BROWSER_LOCO_OTHER_ROW_KEYS = [1, 2, 3, 'continueWatching']
BROWSER_LOCO_SUMMARY_FIELDS = [
    'availability', 'bbSupplementalMessage', 'bbSupplementalMessageIcon',
    'maturity', 'mostWatchedData', 'summary'
]
BROWSER_LOCO_CURRENT_FIELDS = ['hasAudioDescription', 'summary']
BROWSER_LOCO_CONTINUE_FIELDS = ['bookmarkPosition', 'runtime', 'summary', 'title']
BROWSER_LOCO_REFERENCE_FIELDS = ['availability', 'episodeCount', 'inRemindMeList', 'queue', 'summary']
BROWSER_GENRE_SUBGENRE_FIELDS = ['id', 'name', 'unifiedEntityId']
BROWSER_LOCO_DIRECT_RANGE = {'from': 0, 'to': PATH_REQUEST_SIZE_MAX}
SEARCH_GRAPHQL_PAGE_SIZE = 48


def _value(value):
    return {'value': value}


def _summary(video_id, title, video_type, number=None, length=None):
    data = {'id': int(video_id), 'type': video_type, 'name': title}
    if number is not None:
        data['season' if video_type == 'season' else 'episode'] = number
        data['shortName'] = str(number)
    if length is not None:
        data['length'] = length
    return _value(data)


def _graphql_headers():
    headers = {
        'Accept': '*/*',
        'Content-Type': 'application/json',
        'Origin': 'https://www.netflix.com',
        'Referer': 'https://www.netflix.com/browse',
        'x-netflix.nq.stack': 'prod',
        'x-netflix.request.client.user.guid': G.LOCAL_DB.get_active_profile_guid()
    }
    for header, key in (
            ('X-Netflix.browserVersion', 'browser_info_version'),
            ('X-Netflix.osName', 'browser_info_os_name'),
            ('X-Netflix.osVersion', 'browser_info_os_version'),
            ('X-Netflix.uiVersion', 'ui_version')):
        value = G.LOCAL_DB.get_value(key, '', table=('session', ['Name', 'Value']))
        if value:
            headers[header] = value
    return headers


def _season_node_to_item(node, index):
    season_id = str(node['videoId'])
    title = node.get('title') or f'Season {index + 1}'
    episodes = node.get('episodes', {}).get('totalCount')
    return season_id, {
        'summary': _summary(season_id, title, 'season', index + 1, episodes),
        'title': _value(title),
        'availability': _value({'isPlayable': True})
    }


def _episode_node_to_item(node, season_number, metadata=None):
    episode_id = str(node['videoId'])
    metadata = metadata or {}
    synopsis = metadata.get('synopsis') or (node.get('contextualSynopsis') or {}).get('text') or ''
    runtime = metadata.get('runtime') or node.get('runtimeSec') or node.get('displayRuntimeSec') or 0
    bookmark = metadata.get('bookmark') or node.get('bookmark') or {}
    bookmark_position = bookmark.get('offset') or bookmark.get('bookmarkPosition') or 0
    credits_offset = metadata.get('creditsOffset') or metadata.get('watchedToEndOffset') or 0
    watched_threshold = credits_offset - (runtime / 3000 * 50) if credits_offset else runtime * 0.9
    graph_playcount = 1 if 0 < watched_threshold <= bookmark_position else 0
    artwork = node.get('artwork') or {}
    image_url = ''
    if isinstance(artwork, dict):
        image_url = artwork.get('url') or (artwork.get('image') or {}).get('url') or ''
    return episode_id, {
        'summary': _summary(episode_id, node.get('title') or '', 'episode', node.get('number')),
        'title': _value(node.get('title') or ''),
        'synopsis': _value(synopsis),
        'regularSynopsis': _value(synopsis),
        'runtime': _value(runtime),
        'availability': _value({'isPlayable': bool(node.get('isPlayable', True))}),
        'bookmarkPosition': _value(bookmark_position),
        'creditsOffset': _value(metadata.get('creditsOffset', 0)),
        'watchedToEndOffset': _value(metadata.get('watchedToEndOffset', 0)),
        'watched': _value(bool(bookmark.get('watchedDate'))),
        '_graphql_playcount': _value(graph_playcount),
        'interestingMoment': {'_1920x1080': {'jpg': {'value': {'url': image_url}}}},
        'season': _value(season_number)
    }


def _search_graphql_artwork_params():
    return {
        'artworkType': 'SDP',
        'dimension': {'width': 342, 'height': 192},
        'features': {'fallbackStrategy': 'STILL'}
    }


def _search_graphql_game_artwork_params(artwork_type, top_content_type_badge):
    return {
        'artworkType': artwork_type,
        'dimension': {'width': 342, 'height': 192},
        'features': {'fallbackStrategy': 'STILL', 'topContentTypeBadge': top_content_type_badge}
    }


def _search_graphql_options():
    entity_treatments = {
        'pinotStandardBoxshot': {'base': {'canHandleEntityKinds': ['VIDEO']}},
        'pinotStandardCloudAppIcon': {'base': {'canHandleEntityKinds': ['GAME']}},
        'pinotStandardMobileAppIcon': {'base': {'canHandleEntityKinds': ['GAME']}},
        'pinotStandardDestination': {'base': {'canHandleEntityKinds': ['GENERIC_CONTAINER']}}
    }
    return {
        'pageCapabilities': {'base': {
            'canHandlePlayingCloudGames': False,
            'capabilitiesBySection': {
                'pinotGallery': {'base': {'capabilitiesBySectionTreatment': {
                    'pinotCreatorHome': {'base': {
                        'capabilitiesByEntityTreatment': entity_treatments,
                        'maxTotalEntities': 300
                    }},
                    'pinotStandard': {'base': {
                        'capabilitiesByEntityTreatment': entity_treatments,
                        'maxTotalEntities': 300
                    }}
                }}},
                'pinotList': {'base': {'capabilitiesBySectionTreatment': {
                    'pinotSuggestions': {'base': {
                        'capabilitiesByEntityTreatment': {
                            'pinotSuggestion': {'base': {'canHandleEntityKinds': [
                                'AUTOCOMPLETE', 'VIDEO', 'CHARACTER', 'GENERIC_CONTAINER', 'GENRE', 'PERSON'
                            ]}}
                        },
                        'maxTotalEntities': 100
                    }}
                }}}
            },
            'maxTotalSections': 2
        }},
        'session': {'id': str(uuid.uuid4())}
    }


def _search_graphql_variables(search_term, end_cursor=None):
    return {
        'imageParamsForStandardBoxart': _search_graphql_artwork_params(),
        'imageParamsForCloudGameBoxart': _search_graphql_game_artwork_params(
            'GAME_CLOUD_BOXART_HORIZONTAL_INCOMPATIBLE', True),
        'imageParamsForMobileGameBoxart': _search_graphql_game_artwork_params(
            'GAME_ICON_BOXART_HORIZONTAL_CARD', True),
        'pageSize': SEARCH_GRAPHQL_PAGE_SIZE,
        'options': _search_graphql_options(),
        'searchTerm': search_term,
        'selectedSuggestionId': None,
        'endCursor': end_cursor
    }


def _merge_search_metadata_video(base_video, metadata_video):
    merged = dict(base_video)
    metadata_video = metadata_video or {}
    title = metadata_video.get('title') or merged.get('title', {}).get('value')
    if title:
        merged['title'] = _value(title)
        summary = merged.get('summary', {}).get('value', {})
        if isinstance(summary, dict):
            summary['name'] = title
            merged['summary'] = _value(summary)
    synopsis = metadata_video.get('synopsis') or metadata_video.get('regularSynopsis')
    if synopsis:
        merged['synopsis'] = _value(synopsis)
        merged['regularSynopsis'] = _value(synopsis)
    runtime = metadata_video.get('runtime')
    if runtime:
        merged['runtime'] = _value(runtime)
    release_year = metadata_video.get('year') or metadata_video.get('releaseYear')
    if release_year:
        merged['releaseYear'] = _value(release_year)
    seasons = metadata_video.get('seasons') or []
    if seasons:
        merged['seasonCount'] = _value(len(seasons))
        episode_count = sum(len(season.get('episodes') or []) for season in seasons)
        if episode_count:
            merged['episodeCount'] = _value(episode_count)
    return merged


def _search_graphql_node_to_item(node):
    entity = node.get('unifiedEntity') or {}
    entity_type = entity.get('__typename')
    if entity_type not in ('Movie', 'Show'):
        return None
    video_id = str(entity.get('videoId') or '')
    if not video_id:
        return None
    video_type = 'movie' if entity_type == 'Movie' else 'show'
    title = node.get('displayString') or str(video_id)
    item = {
        'summary': _summary(video_id, title, video_type),
        'title': _value(title),
        'availability': _value({'isPlayable': True}),
        'queue': _value({'inQueue': False}),
        'inRemindMeList': _value(False),
        'bookmarkPosition': _value(0),
        'creditsOffset': _value(0),
        'watchedToEndOffset': _value(0),
        'watched': _value(False),
        'runtime': _value(entity.get('runtimeSec', 0)),
        'releaseYear': _value(entity.get('releaseYear', 0)),
        'maturity': _value(entity.get('contentAdvisory', {})),
        'trackIds': _value({}),
        'requestId': _value('')
    }
    artwork = (node.get('contextualArtwork') or {}).get('artwork') or {}
    if artwork.get('url'):
        _set_browser_boxart(item, {'id': int(video_id), 'title': title, 'boxArt': {'url': artwork['url']}})
    return video_id, item


def _normalize_browser_list_lengths(path_response):
    for list_data in path_response.get('lists', {}).values():
        if not isinstance(list_data, dict):
            continue
        length = list_data.get('componentSummary', {}).get('value', {}).get('length')
        if not isinstance(length, int):
            continue
        for key in list(list_data.keys()):
            if common.is_numeric(key) and int(key) >= length:
                del list_data[key]


def _set_browser_boxart(video, item_summary):
    boxart = item_summary.get('boxArt') or {}
    image_url = boxart.get('url')
    if not image_url:
        return
    art_value = {'url': image_url}
    video.setdefault('itemSummary', _value(item_summary))
    video.setdefault('boxarts', {})
    for size in (ART_SIZE_SD, ART_SIZE_FHD, ART_SIZE_POSTER):
        video['boxarts'].setdefault(size, {'jpg': {'value': art_value}})
    video.setdefault('interestingMoment', {ART_SIZE_FHD: {'jpg': {'value': art_value}}})


def _normalize_browser_video_fields(path_response):
    _normalize_browser_list_lengths(path_response)
    item_summaries = {}
    for list_data in path_response.get('lists', {}).values():
        if not isinstance(list_data, dict):
            continue
        for item in list_data.values():
            if not isinstance(item, dict):
                continue
            item_summary = item.get('itemSummary', {}).get('value', {})
            ref = item.get('reference', {})
            ref_value = ref.get('value') if isinstance(ref, dict) else ref
            if isinstance(ref_value, dict) and 'value' in ref_value:
                ref_value = ref_value['value']
            if isinstance(ref_value, list) and len(ref_value) >= 2 and ref_value[0] == 'videos':
                item_summaries[str(ref_value[1])] = item_summary
    for video_id, video in path_response.get('videos', {}).items():
        if not isinstance(video, dict):
            continue
        item_summary = item_summaries.get(str(video_id), {})
        if item_summary:
            video.setdefault('itemSummary', _value(item_summary))
            _set_browser_boxart(video, item_summary)
        summary = video.get('summary', {}).get('value', {})
        if not isinstance(summary, dict):
            summary = {}
        title_data = video.get('title')
        title_value = title_data.get('value') if isinstance(title_data, dict) else title_data
        if not title_value:
            nested_title = summary.get('title')
            if isinstance(nested_title, dict):
                title_value = nested_title.get('value')
            elif nested_title:
                title_value = nested_title
            else:
                title_value = (item_summary.get('title') or item_summary.get('name') or
                               item_summary.get('displayName') or summary.get('name') or str(video_id))
            video['title'] = _value(title_value)
        if title_value and isinstance(summary, dict):
            summary.setdefault('name', title_value)
        synopses = item_summary.get('synopses') or {}
        synopsis = synopses.get('regularSynopsis') or synopses.get('shortSynopsis') or synopses.get('narrative')
        if synopsis:
            video.setdefault('synopsis', _value(synopsis))
            video.setdefault('regularSynopsis', _value(synopsis))
        video.setdefault('availability', _value(item_summary.get('availability', {'isPlayable': True})))
        video.setdefault('queue', _value({'inQueue': False}))
        video.setdefault('inRemindMeList', _value(False))
        video.setdefault('bookmarkPosition', _value(0))
        video.setdefault('creditsOffset', _value(0))
        video.setdefault('watchedToEndOffset', _value(0))
        video.setdefault('watched', _value(False))
        video.setdefault('runtime', _value(summary.get('runtime', item_summary.get('runtime', item_summary.get('infoDensityRuntime', 0)))))
        video.setdefault('releaseYear', _value(item_summary.get('releaseYear', 0)))
        video.setdefault('seasonCount', _value(item_summary.get('seasonCount', 0)))
        video.setdefault('episodeCount', _value(item_summary.get('episodeCount', 0)))
        video.setdefault('maturity', _value(item_summary.get('maturity', {})))
        video.setdefault('trackIds', _value({}))
        video.setdefault('requestId', _value(item_summary.get('requestId', '')))

if TYPE_CHECKING:  # This variable/imports are used only by the editor, so not at runtime
    from resources.lib.services.nfsession.nfsession_ops import NFSessionOperations


class DirectoryPathRequests:
    """Builds and executes PATH requests for the directories"""

    def __init__(self, nfsession: 'NFSessionOperations'):
        self.nfsession = nfsession

    @cache_utils.cache_output(cache_utils.CACHE_MYLIST, fixed_identifier='my_list_items', ignore_self_class=True)
    def req_mylist_items(self):
        """Return the 'my list' video list as videoid items"""
        LOG.debug('Requesting "my list" video list as videoid items')
        try:
            items = []
            video_list = self.req_datatype_video_list_full(G.MAIN_MENU_ITEMS['myList']['request_context_name'])
            if video_list:
                # pylint: disable=unused-variable
                items = [common.VideoId.from_videolist_item(video)
                         for video_id, video in video_list.videos.items()
                         if video['queue']['value'].get('inQueue', False)]
            return items
        except InvalidVideoListTypeError:
            return []
        except req_exceptions.HTTPError as exc:
            if getattr(exc.response, 'status_code', None) != 404:
                raise
            LOG.warn('My List marker lookup disabled because pathEvaluator returned 404')
            return []

    @cache_utils.cache_output(cache_utils.CACHE_COMMON, fixed_identifier='loco_list', ignore_self_class=True)
    def req_loco_list_root(self):
        """Retrieve root LoCo list"""
        # It is used to following cases:
        # - To get items for the main menu
        #      (when 'loco_known'==True and loco_contexts is set, see MAIN_MENU_ITEMS in globals.py)
        # - To get list items for menus that have multiple contexts set to 'loco_contexts' like 'recommendations' menu
        LOG.debug('Requesting LoCo root lists')
        paths = ([['loco', 'componentSummary'],
                  ['loco', {'from': 0, 'to': 50}, 'componentSummary'],
                  # Titles of first 4 videos in each video list (needed only to show titles in the plot description)
                  ['loco', {'from': 0, 'to': 50}, {'from': 0, 'to': 3}, 'reference', ['title', 'summary']]] +
                 # Art for the first video of each context list (needed only to add art to the menu item)
                 build_paths(['loco', {'from': 0, 'to': 50}, 0, 'reference'], ART_PARTIAL_PATHS))
        call_args = {'paths': paths}
        try:
            path_response = self.nfsession.path_request(**call_args)
        except req_exceptions.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            LOG.warn('Falling back to empty LoCo root menu after pathEvaluator 404')
            path_response = {'locos': {'root': {'componentSummary': _value({'length': 0})}}, 'lists': {}}
        return LoCo(path_response)

    @cache_utils.cache_output(cache_utils.CACHE_GENRES, identify_from_kwarg_name='genre_id', ignore_self_class=True)
    def req_loco_list_genre(self, genre_id):
        """Retrieve LoCo for the given genre"""
        LOG.debug('Requesting LoCo for genre {}', genre_id)
        return self._req_browser_genre_loco(genre_id)

    def get_loco_list_id_by_context(self, context):
        """Return the dynamic video list ID for a LoCo context"""
        try:
            return next(iter(self.req_loco_list_root().lists_by_context([context], True)))[0]
        except StopIteration as exc:
            raise InvalidVideoListTypeError(f'No lists with context {context} available') from exc

    @cache_utils.cache_output(cache_utils.CACHE_COMMON, fixed_identifier='profiles_raw_data',
                              ttl=300, ignore_self_class=True)
    def req_profiles_info(self, update_database=True):
        """Retrieve raw data of the profiles (and save it to the database)"""
        paths = ([['profilesList', 'summary'],
                  ['profilesList', 'current', 'summary'],
                  ['profilesList', {'to': 5}, 'summary'],
                  ['profilesList', {'to': 5}, 'avatar', 'images', 'byWidth', 320]])
        path_response = self.nfsession.path_request(paths)
        if update_database:
            from resources.lib.utils.website import parse_profiles
            parse_profiles(path_response)
        return path_response

    @cache_utils.cache_output(cache_utils.CACHE_COMMON, identify_append_from_kwarg_name='perpetual_range_start',
                              ignore_self_class=True)
    def req_seasons(self, videoid, perpetual_range_start):
        """Retrieve the seasons of a tv show"""
        if videoid.mediatype != common.VideoId.SHOW:
            raise InvalidVideoId(f'Cannot request season list for {videoid}')
        LOG.debug('Requesting the seasons list for show {}', videoid)
        call_args = {
            'paths': (build_paths(['videos', videoid.tvshowid], SEASONS_PARTIAL_PATHS) +
                      build_paths(['videos', videoid.tvshowid], ART_PARTIAL_PATHS) +
                      [['videos', videoid.tvshowid, 'componentSummary']]),
            'length_params': ['stdlist_wid', ['videos', videoid.tvshowid, 'seasonList']],
            'perpetual_range_start': perpetual_range_start
        }
        try:
            path_response = self.nfsession.perpetual_path_request(**call_args)
            return SeasonList(videoid, path_response)
        except req_exceptions.HTTPError as exc:
            if getattr(exc.response, 'status_code', None) != 404:
                raise
            LOG.warn('Falling back to GraphQL season selector for show {}', videoid.tvshowid)
            return self._req_seasons_graphql(videoid)

    @cache_utils.cache_output(cache_utils.CACHE_COMMON, identify_from_kwarg_name='videoid',
                              identify_append_from_kwarg_name='perpetual_range_start', ignore_self_class=True)
    def req_episodes(self, videoid, perpetual_range_start=None):
        """Retrieve the episodes of a season"""
        if videoid.mediatype != common.VideoId.SEASON:
            raise InvalidVideoId(f'Cannot request episode list for {videoid}')
        LOG.debug('Requesting episode list for {}', videoid)
        paths = ([['seasons', videoid.seasonid, 'summary']] +
                 [['seasons', videoid.seasonid, 'componentSummary']] +
                 build_paths(['seasons', videoid.seasonid, 'episodes', RANGE_PLACEHOLDER], EPISODES_PARTIAL_PATHS) +
                 build_paths(['videos', videoid.tvshowid], ART_PARTIAL_PATHS + [[['title', 'delivery']]]))
        call_args = {
            'paths': paths,
            'length_params': ['stdlist_wid', ['seasons', videoid.seasonid, 'episodes']],
            'perpetual_range_start': perpetual_range_start
        }
        try:
            path_response = self.nfsession.perpetual_path_request(**call_args)
            return EpisodeList(videoid, path_response)
        except req_exceptions.HTTPError as exc:
            if getattr(exc.response, 'status_code', None) != 404:
                raise
            LOG.warn('Falling back to GraphQL episode selector for season {}', videoid.seasonid)
            return self._req_episodes_graphql(videoid)

    def _post_graphql(self, operation_name, variables, operation_id):
        payload = {
            'operationName': operation_name,
            'variables': variables,
            'extensions': {'persistedQuery': {'id': operation_id, 'version': 102}}
        }
        response = self.nfsession.session.post(
            GRAPHQL_URL,
            json=payload,
            headers=_graphql_headers(),
            timeout=8)
        response.raise_for_status()
        return response.json()['data']

    def _req_seasons_graphql(self, videoid):
        data = self._post_graphql(
            'PreviewModalEpisodeSelector',
            {'showId': int(videoid.tvshowid), 'seasonCount': 50},
            GRAPHQL_OP_SEASONS)
        show_data = data['videos'][0]
        edges = show_data['seasons']['edges']
        seasons = OrderedDict(
            _season_node_to_item(edge.get('node') or edge, index)
            for index, edge in enumerate(edges))
        show_title = self._metadata_show_title(videoid) or show_data.get('title') or str(videoid.tvshowid)
        tvshow = {
            'title': _value(show_title),
            'delivery': _value({}),
            'seasonList': {'summary': _value({'length': len(seasons)})}
        }
        return SimpleNamespace(
            perpetual_range_selector=None,
            data={'videos': {videoid.tvshowid: tvshow}, 'seasons': seasons},
            videoid=videoid,
            artitem=tvshow,
            tvshow=tvshow,
            seasons=seasons)

    def _metadata_show_title(self, videoid):
        try:
            metadata_data = self.nfsession.get_safe(
                endpoint='metadata',
                params={'movieid': videoid.tvshowid, '_': int(time.time() * 1000)})
            return metadata_data['video'].get('title') or ''
        except (MetadataNotAvailable, KeyError, TypeError, req_exceptions.RequestException):
            return ''

    def _metadata_episodes_by_id(self, videoid):
        try:
            metadata_data = self.nfsession.get_safe(
                endpoint='metadata',
                params={'movieid': videoid.tvshowid, '_': int(time.time() * 1000)})
            show_metadata = metadata_data['video']
        except (MetadataNotAvailable, KeyError, TypeError, req_exceptions.RequestException):
            return {}
        episodes = {}
        for season in show_metadata.get('seasons', []):
            if str(season.get('id')) != videoid.seasonid:
                continue
            for episode in season.get('episodes', []):
                episodes[str(episode.get('id'))] = episode
            break
        return episodes


    def _req_episodes_graphql(self, videoid):
        data = self._post_graphql(
            'PreviewModalEpisodeSelectorSeasonEpisodes',
            {
                'seasonId': int(videoid.seasonid),
                'count': 50,
                'opaqueImageFormat': 'JPG',
                'artworkContext': {}
            },
            GRAPHQL_OP_EPISODES)
        season_data = data['videos'][0]
        season_number = season_data.get('number')
        edges = season_data['episodes']['edges']
        metadata_by_id = self._metadata_episodes_by_id(videoid)
        episodes = OrderedDict(
            _episode_node_to_item(edge.get('node') or edge, season_number,
                                  metadata_by_id.get(str((edge.get('node') or edge).get('videoId'))))
            for edge in edges)
        show_title = self._metadata_show_title(videoid) or str(videoid.tvshowid)
        tvshow = {
            'title': _value(show_title),
            'delivery': _value({})
        }
        season = {
            'summary': _summary(videoid.seasonid, season_data.get('title') or '', 'season', season_number, len(episodes)),
            'title': _value(season_data.get('title') or '')
        }
        return SimpleNamespace(
            perpetual_range_selector=None,
            data={'videos': {videoid.tvshowid: tvshow}, 'seasons': {videoid.seasonid: season}, 'episodes': episodes},
            videoid=videoid,
            tvshow=tvshow,
            season=season,
            episodes=episodes)

    def _browse_html_and_auth_url(self):
        browse_html = self.nfsession.get_safe('browse')
        api_data = self.nfsession.website_extract_session_data(browse_html)
        self.nfsession.auth_url = api_data['auth_url']
        browse_text = browse_html.decode('utf-8', 'replace') if isinstance(browse_html, bytes) else browse_html
        return browse_text, api_data['auth_url']

    def _get_current_loco_root_id(self):
        browse_html, auth_url = self._browse_html_and_auth_url()
        match = LOCO_ROOT_ID_RE.search(browse_html)
        if not match:
            raise InvalidVideoListTypeError('No current LoCo root id found in browse page')
        return match.group(0), auth_url

    def _current_loco_paths(self, root_id):
        return ([
            ['locos', root_id, 'componentSummary'],
            ['locos', root_id, LOCO_ROW_RANGE, 'componentSummary'],
            ['locos', root_id, LOCO_ROW_RANGE, 'page', 0, LOCO_PAGE_RANGE, 'itemSummary'],
            ['locos', root_id, LOCO_ROW_RANGE, 'page', 0, LOCO_PAGE_RANGE, 'reference', LOCO_REFERENCE_FIELDS]
        ] + build_paths(
            ['locos', root_id, LOCO_ROW_RANGE, 'page', 0, LOCO_PAGE_RANGE, 'reference'],
            ART_PARTIAL_PATHS))

    def _post_current_loco_paths(self, paths, auth_url):
        self.nfsession.auth_url = auth_url
        return self._post_browser_path_evaluator(paths, 'https://www.netflix.com/browse')

    def _post_browser_path_evaluator(self, paths, referer):
        api_url = G.LOCAL_DB.get_value(
            'api_endpoint_url',
            'https://www.netflix.com/nq/website/memberapi/release',
            table=TABLE_SESSION)
        form_data = [('path', json.dumps(path, separators=(',', ':'))) for path in paths]
        form_data.append(('authURL', self.nfsession.auth_url))
        response = self.nfsession.session.post(
            f'{api_url}/pathEvaluator',
            params={
                'webp': 'false',
                'drmSystem': 'widevine',
                'isVolatileBillboardsEnabled': 'true',
                'isTop10Supported': 'true',
                'hasVideoMerchInBob': 'false',
                'hasVideoMerchInJaw': 'false',
                'falcor_server': '0.1.0',
                'withSize': 'true',
                'materialize': 'true',
                'original_path': '/shakti/mre/pathEvaluator'
            },
            data=urlencode(form_data),
            headers={
                'Accept': '*/*',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://www.netflix.com',
                'Referer': referer,
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'x-netflix.nq.stack': 'prod',
                'x-netflix.request.client.context': 'www.netflix.com',
                'x-netflix.request.client.user.guid': G.LOCAL_DB.get_active_profile_guid()
            },
            timeout=8)
        response.raise_for_status()
        path_response = response.json()['jsonGraph']
        _normalize_browser_video_fields(path_response)
        return path_response

    def _browser_loco_paths(self, root_path, include_genre_paths=False, include_full_rows=False):
        paths = [
            root_path + [['componentSummary', 'debugRequest']],
            root_path + [BROWSER_LOCO_ROW_KEYS, 'componentSummary'],
            root_path + ['meta', ['responseExpiration', 'statusCode']],
            root_path + [0, 0, 'itemSummary'],
            root_path + [0, 0, 'reference', BROWSER_LOCO_SUMMARY_FIELDS],
            root_path + [0, 0, 'reference', 'current', BROWSER_LOCO_CURRENT_FIELDS],
            root_path + [0, 'page', 0, LOCO_PAGE_RANGE, 'itemSummary'],
            root_path + [BROWSER_LOCO_OTHER_ROW_KEYS, 'page', 0, LOCO_PAGE_RANGE, 'itemSummary'],
            root_path + [BROWSER_LOCO_ROW_KEYS, 'page', 0, LOCO_PAGE_RANGE, 'reference', BROWSER_LOCO_REFERENCE_FIELDS],
            root_path + ['continueWatching', 'page', 0, LOCO_PAGE_RANGE, 'reference', 'current',
                         BROWSER_LOCO_CONTINUE_FIELDS]
        ]
        if include_full_rows:
            paths.extend([
                root_path + [BROWSER_LOCO_ROW_KEYS, BROWSER_LOCO_DIRECT_RANGE, 'itemSummary'],
                root_path + [BROWSER_LOCO_ROW_KEYS, BROWSER_LOCO_DIRECT_RANGE, 'reference', BROWSER_LOCO_REFERENCE_FIELDS]
            ])
        if include_genre_paths:
            paths.insert(0, root_path[:-1] + [['name', 'trackIds']])
        return paths

    def _browser_video_list_paths(self, list_id):
        return [
            ['lists', list_id, ['componentSummary', 'debugRequest']],
            ['lists', list_id, 'page', 0, LOCO_PAGE_RANGE, 'itemSummary'],
            ['lists', list_id, 'page', 0, LOCO_PAGE_RANGE, 'reference', LOCO_REFERENCE_FIELDS]
        ]

    def _req_browser_lolomo_category(self, category_name):
        self._browse_html_and_auth_url()
        path_response = self._post_browser_path_evaluator(
            self._browser_loco_paths(['lolomoByCategory', category_name]),
            'https://www.netflix.com/latest')
        return LoLoMoCategory(path_response)

    def _req_browser_genre_loco(self, genre_id):
        self._browse_html_and_auth_url()
        path_response = self._post_browser_path_evaluator(
            self._browser_loco_paths(['genres', int(genre_id), 'rw'], include_genre_paths=True),
            f'https://www.netflix.com/browse/genre/{genre_id}')
        return LoCo(path_response)

    def _browser_video_list_by_id(self, list_id):
        self._browse_html_and_auth_url()
        path_response = self._post_browser_path_evaluator(
            self._browser_video_list_paths(str(list_id)),
            'https://www.netflix.com/browse')
        return VideoList(path_response, str(list_id))

    def _browser_lolomo_video_list_by_id(self, category_name, list_id):
        self._browse_html_and_auth_url()
        path_response = self._post_browser_path_evaluator(
            self._browser_loco_paths(['lolomoByCategory', category_name], include_full_rows=True),
            'https://www.netflix.com/latest')
        if str(list_id) not in path_response.get('lists', {}):
            raise InvalidVideoListTypeError(f'No LoLoMo category list with id {list_id}')
        return VideoList(path_response, str(list_id))

    def _browser_genre_video_list_by_id(self, genre_id, list_id):
        self._browse_html_and_auth_url()
        path_response = self._post_browser_path_evaluator(
            self._browser_loco_paths(['genres', int(genre_id), 'rw'], include_genre_paths=True, include_full_rows=True),
            f'https://www.netflix.com/browse/genre/{genre_id}')
        if str(list_id) not in path_response.get('lists', {}):
            raise InvalidVideoListTypeError(f'No genre list with id {list_id}')
        return VideoList(path_response, str(list_id))

    def _first_loco_video_list(self, loco):
        for _list_id, video_list in loco.lists.items():
            if video_list.videos:
                return video_list
        return next(iter(loco.lists.values()))

    def _req_current_loco_root_data(self):
        root_id, auth_url = self._get_current_loco_root_id()
        return self._post_current_loco_paths(self._current_loco_paths(root_id), auth_url)

    def _current_loco_list_by_context(self, context):
        loco = LoCo(self._req_current_loco_root_data())
        list_id, video_list = loco.find_by_context(context)
        if not list_id:
            category_contexts = LOCO_CATEGORY_CONTEXTS.get('comingSoon', ())
            if context in category_contexts:
                for _list_id, summary, category_video_list in self.req_lolomo_category('comingSoon').lists():
                    if summary.get('context') == context:
                        return category_video_list
            raise InvalidVideoListTypeError(f'No current LoCo list with context {context} available')
        return video_list

    def _current_loco_list_by_id(self, list_id):
        loco = LoCo(self._req_current_loco_root_data())
        if str(list_id) not in loco.data.get('lists', {}):
            raise InvalidVideoListTypeError(f'No current LoCo list with id {list_id} available')
        return VideoList(loco.data, str(list_id))

    def _current_lolomo_category(self, category_name):
        contexts = LOCO_CATEGORY_CONTEXTS.get(category_name)
        if not contexts:
            raise InvalidVideoListTypeError(f'No current LoCo fallback for category {category_name}')
        loco = LoCo(self._req_current_loco_root_data())
        lists = OrderedDict(
            (list_id, list_data)
            for list_id, list_data in loco.data.get('lists', {}).items()
            if list_data.get('componentSummary', {}).get('value', {}).get('context') in contexts)
        root_id = loco.id
        root = OrderedDict()
        root['componentSummary'] = _value({'length': len(lists)})
        for index, list_id in enumerate(lists):
            root[index] = {
                'reference': _value(['lists', list_id]),
                'itemSummary': _value({'id': list_id})
            }
        return LoLoMoCategory({
            'locos': {root_id: root},
            'lists': lists,
            'videos': loco.data.get('videos', {})
        })


    @cache_utils.cache_output(cache_utils.CACHE_COMMON, identify_append_from_kwarg_name='perpetual_range_start',
                              ignore_self_class=True)
    def req_video_list(self, list_id, perpetual_range_start=None, menu_data=None):
        """Retrieve a video list"""
        # Some of this type of request have results fixed at ~40 from netflix
        # The 'length' tag never return to the actual total count of the elements
        LOG.debug('Requesting video list {}', list_id)
        paths = (build_paths(['lists', list_id, RANGE_PLACEHOLDER, 'reference'], VIDEO_LIST_PARTIAL_PATHS) +
                 [['lists', list_id, 'componentSummary']])
        call_args = {
            'paths': paths,
            'length_params': ['stdlist', ['lists', list_id]],
            'perpetual_range_start': perpetual_range_start
        }
        try:
            path_response = self.nfsession.perpetual_path_request(**call_args)
        except req_exceptions.HTTPError as exc:
            if getattr(exc.response, 'status_code', None) != 404:
                raise
            initial_menu_id = (menu_data or {}).get('initial_menu_id')
            if initial_menu_id in ('newAndPopular', 'recommendations'):
                LOG.warn('Falling back to browser-shaped LoLoMo category list {} after pathEvaluator 404', list_id)
                return self._browser_lolomo_video_list_by_id('comingSoon', list_id)
            LOG.warn('Falling back to browser-shaped list {} after pathEvaluator 404', list_id)
            try:
                return self._browser_video_list_by_id(list_id)
            except req_exceptions.HTTPError:
                return self._current_loco_list_by_id(list_id)
        return VideoList(path_response)

    @cache_utils.cache_output(cache_utils.CACHE_COMMON, identify_from_kwarg_name='context_id',
                              identify_append_from_kwarg_name='perpetual_range_start', ignore_self_class=True)
    def req_video_list_sorted(self, context_name, context_id=None, perpetual_range_start=None, menu_data=None):
        """Retrieve a video list sorted"""
        # This type of request allows to obtain more than ~40 results
        LOG.debug('Requesting video list sorted for context name: "{}", context id: "{}"',
                  context_name, context_id)
        base_path = [context_name]
        response_type = 'stdlist'
        if context_id:
            base_path.append(context_id)
            response_type = 'stdlist_wid'

        # enum order: AZ|ZA|Suggested|Year
        # sort order the "mylist" is supported only in US country, the only way to query is use 'az'
        sort_order_types = ['az', 'za', 'su', 'yr'] if not context_name == 'mylist' else ['az', 'az']
        req_sort_order_type = sort_order_types[
            int(G.ADDON.getSettingInt('menu_sortorder_' + menu_data.get('initial_menu_id', menu_data['path'][1])))
        ]
        base_path.append(req_sort_order_type)
        _base_path = list(base_path)
        _base_path.append(RANGE_PLACEHOLDER)
        if not menu_data.get('query_without_reference', False):
            _base_path.append('reference')
        paths = (build_paths(_base_path, VIDEO_LIST_PARTIAL_PATHS) +
                 [base_path[:-1] + [['id', 'name', 'requestId', 'trackIds']]])

        try:
            path_response = self.nfsession.perpetual_path_request(paths, [response_type, base_path], perpetual_range_start)
        except req_exceptions.HTTPError as exc:
            if getattr(exc.response, 'status_code', None) != 404:
                raise
            context = SORTED_LIST_CONTEXT_FALLBACKS.get((context_name, str(context_id)))
            if not context:
                raise
            LOG.warn('Falling back to browser-shaped genre {} after pathEvaluator 404', context_id)
            return self._first_loco_video_list(self._req_browser_genre_loco(context_id))
        return VideoListSorted(path_response, context_name, context_id, req_sort_order_type)


    @cache_utils.cache_output(cache_utils.CACHE_COMMON, identify_from_kwarg_name='context_id',
                              identify_append_from_kwarg_name='perpetual_range_start', ignore_self_class=True)
    def req_videos_list_sorted(self, context_name, context_id=None, perpetual_range_start=None, menu_data=None):
        """Retrieve a video's list sorted"""
        # This type of request allows to obtain more than ~40 results
        LOG.debug('Requesting video\'s list sorted for context name: "{}", context id: "{}"',
                  context_name, context_id)
        base_path = [context_name]
        response_type = 'videoslist'
        if context_id:
            base_path.append(context_id)

        # enum order: AZ|ZA|Suggested|Year
        # sort order the "mylist" is supported only in US country, the only way to query is use 'az'
        sort_order_types = ['az', 'za', 'su', 'yr'] if context_name != 'mylist' else ['az', 'az']
        req_sort_order_type = sort_order_types[
            int(G.ADDON.getSettingInt('menu_sortorder_' + menu_data.get('initial_menu_id', menu_data['path'][1])))
        ]
        base_path.append(req_sort_order_type)
        _base_path = list(base_path)
        _base_path.append(RANGE_PLACEHOLDER)
        if not menu_data.get('query_without_reference', False):
            _base_path.append('reference')
        paths = (build_paths(_base_path, VIDEO_LIST_PARTIAL_PATHS) +
                 [base_path[:-1] + [['id', 'name', 'requestId', 'trackIds']]])

        path_response = self.nfsession.perpetual_path_request(paths, [response_type, ['videos']], perpetual_range_start)
        return VideosList(path_response, [context_name, context_id])

    @cache_utils.cache_output(cache_utils.CACHE_SUPPLEMENTAL, identify_append_from_kwarg_name='supplemental_type',
                              ignore_self_class=True)
    def req_video_list_supplemental(self, videoid, supplemental_type):
        """Retrieve a video list of supplemental type videos"""
        if videoid.mediatype not in (common.VideoId.SHOW, common.VideoId.MOVIE):
            raise InvalidVideoId(f'Cannot request video list supplemental for {videoid}')
        LOG.debug('Requesting video list supplemental of type "{}" for {}', supplemental_type, videoid)
        path = build_paths(
            ['videos', videoid.value, supplemental_type, {"from": 0, "to": 35}], TRAILER_PARTIAL_PATHS
        )
        def _empty_fallback():
            return SimpleNamespace(
                perpetual_range_selector=None,
                videos=OrderedDict(),
                artitem=None,
                contained_titles=[],
                component_summary={})

        def _similars_fallback():
            try:
                path_response = self.nfsession.path_request(
                    [['videos', int(videoid.value), 'similars', {'from': 0, 'to': 35}, 'summary']])
            except req_exceptions.HTTPError as exc:
                if getattr(exc.response, 'status_code', None) != 404:
                    raise
                LOG.warn('Similar-title fallback returned 404 for {}', videoid)
                return _empty_fallback()
            similar_items = common.get_path_safe(['videos', videoid.value, 'similars'], path_response, None, {})
            videos = OrderedDict()
            if isinstance(similar_items, dict):
                iterable_items = similar_items.values()
            else:
                iterable_items = similar_items if isinstance(similar_items, list) else []
            for item in iterable_items:
                summary = item.get('value') if isinstance(item, dict) else None
                if not isinstance(summary, dict) or not summary.get('id'):
                    continue
                item_id = str(summary['id'])
                title = summary.get('name') or summary.get('title') or item_id
                videos[item_id] = {
                    'title': _value(title),
                    'summary': _value(summary),
                    'availability': _value({'isPlayable': True}),
                    'trackIds': _value({})
                }
                metadata_video = self._metadata_for_search_video(item_id)
                if metadata_video:
                    videos[item_id] = _merge_search_metadata_video(videos[item_id], metadata_video)
            return CustomVideoList({'videos': videos}) if videos else _empty_fallback()

        def _promo_fallback():
            metadata = self.nfsession.get_safe(endpoint='metadata', params={'movieid': videoid.value, '_': int(time.time() * 1000)})
            trailer_id = common.get_path_safe(['video', 'promoVideo', 'value', 'id'], metadata, None)
            if not trailer_id:
                trailer_id = common.get_path_safe(['video', 'merchedVideoId'], metadata, None)
            if not trailer_id:
                LOG.warn('No promo trailer id found for {}, trying similar-title fallback', videoid)
                return _similars_fallback()
            title = common.get_path_safe(['video', 'title'], metadata, 'Trailer')
            return SimpleNamespace(
                perpetual_range_selector=None,
                videos=OrderedDict({str(trailer_id): {
                    'title': {'value': title},
                    'availability': {'value': {'isPlayable': True}},
                    'summary': {'value': {'id': int(trailer_id), 'type': 'movie', 'name': title}},
                    'trackIds': {'value': {'trackId': str(trailer_id)}}
                }}),
                artitem=None,
                contained_titles=[title],
                component_summary={})
        try:
            path_response = self.nfsession.path_request(path)
            trailer_list = VideoListSupplemental(path_response, 'videos', videoid.value, supplemental_type)
            if trailer_list.videos:
                return trailer_list
            LOG.warn('Trailer supplemental response was empty for {}, trying promoVideo fallback', videoid)
            return _promo_fallback()
        except req_exceptions.HTTPError as exc:
            if getattr(exc.response, 'status_code', None) != 404:
                raise
            LOG.warn('Trailer supplemental path returned 404 for {}, trying promoVideo fallback', videoid)
            return _promo_fallback()

    @cache_utils.cache_output(cache_utils.CACHE_COMMON, identify_from_kwarg_name='chunked_video_list',
                              ttl=900, ignore_self_class=True)
    def req_video_list_chunked(self, chunked_video_list, perpetual_range_selector=None):
        """Retrieve a video list which contains the video ids specified"""
        if not any(isinstance(item, list) for item in chunked_video_list):
            raise InvalidVideoListTypeError('The chunked_video_list not contains a list of a list of videoids')
        merged_response = {}
        for videoids_list in chunked_video_list:
            path = build_paths(['videos', videoids_list], VIDEO_LIST_PARTIAL_PATHS)
            path_response = self.nfsession.path_request(path)
            common.merge_dicts(path_response, merged_response)

        if perpetual_range_selector:
            merged_response.update(perpetual_range_selector)
        return CustomVideoList(merged_response)

    def req_video_list_search(self, search_term, perpetual_range_start=None):
        """Retrieve a video list by search term"""
        LOG.debug('Requesting video list by search term "{}"', search_term)
        base_path = ['search', 'byTerm', f'|{search_term}', 'titles', PATH_REQUEST_SIZE_STD]
        paths = ([base_path + [['id', 'name', 'requestId', 'trackIds']]] +
                 build_paths(base_path + [RANGE_PLACEHOLDER, 'reference'], VIDEO_LIST_PARTIAL_PATHS))
        call_args = {
            'paths': paths,
            'length_params': ['searchlist', ['search', 'byReference']],
            'perpetual_range_start': perpetual_range_start
        }
        try:
            path_response = self.nfsession.perpetual_path_request(**call_args)
        except req_exceptions.HTTPError as exc:
            if getattr(exc.response, 'status_code', None) != 404:
                raise
            LOG.warn('Falling back to browser GraphQL search for term "{}" after pathEvaluator 404', search_term)
            return self._req_video_list_search_graphql(search_term)
        return SearchVideoList(path_response)

    def _req_video_list_search_graphql(self, search_term):
        data = self._post_graphql(
            'SearchPageQueryResults',
            _search_graphql_variables(search_term),
            GRAPHQL_OP_SEARCH)
        videos = OrderedDict()
        page = data.get('page') or {}
        sections = (page.get('sections') or {}).get('edges') or []
        for section in sections:
            section_node = section.get('node') or {}
            if section_node.get('__typename') != 'PinotGallerySection':
                continue
            entities = (section_node.get('entities') or {}).get('edges') or []
            for entity_edge in entities:
                item = _search_graphql_node_to_item(entity_edge.get('node') or {})
                if item:
                    video_id, video_data = item
                    videos.setdefault(video_id, video_data)
        for video_id, video_data in list(videos.items()):
            metadata_video = self._metadata_for_search_video(video_id)
            if metadata_video:
                videos[video_id] = _merge_search_metadata_video(video_data, metadata_video)
        return CustomVideoList({'videos': videos})

    def _metadata_for_search_video(self, video_id):
        try:
            metadata_data = self.nfsession.get_safe(
                endpoint='metadata',
                params={'movieid': video_id, '_': int(time.time() * 1000)})
            return metadata_data.get('video') or {}
        except (MetadataNotAvailable, KeyError, TypeError, req_exceptions.RequestException):
            LOG.warn('Search metadata enrichment skipped for video {}', video_id)
            return {}

    def req_subgenres(self, genre_id):
        """Retrieve sub-genres for the given genre"""
        LOG.debug('Requesting sub-genres of the genre {}', genre_id)
        path = [['genres', genre_id, 'subgenres', {'from': 0, 'to': 47}, ['id', 'name']]]
        path_response = self.nfsession.path_request(path)
        return SubgenreList(path_response)

    def req_datatype_video_list_full(self, context_name, switch_profiles=False):
        """
        Retrieve the FULL video list for a context name (no limits to the number of path requests)
        contains only minimal video info
        """
        LOG.debug('Requesting the full video list for {}', context_name)
        paths = (build_paths([context_name, 'az', RANGE_PLACEHOLDER], VIDEO_LIST_BASIC_PARTIAL_PATHS) +
                 [[context_name, ['id', 'name', 'requestId', 'trackIds']]])
        call_args = {
            'paths': paths,
            'length_params': ['stdlist', [context_name, 'az']],
            'perpetual_range_start': None,
            'request_size': PATH_REQUEST_SIZE_MAX,
            'no_limit_req': True
        }
        if switch_profiles:
            # Used only with library auto-update with the sync with Netflix "My List" enabled.
            # It may happen that the user browses the frontend with a different profile used by library sync,
            # and it could cause a wrong query request to nf server.
            # So we try to switch the profile, get My List items and restore previous
            # active profile in a "single call" to try perform the operations in a faster way.
            path_response = self.nfsession.perpetual_path_request_switch_profiles(**call_args)
        else:
            path_response = self.nfsession.perpetual_path_request(**call_args)
        return None if not path_response else VideoListSorted(path_response, context_name, None, 'az')

    def req_datatype_video_list_byid(self, video_ids, custom_partial_paths=None):
        """Retrieve a video list which contains the specified by video ids and return a CustomVideoList object"""
        LOG.debug('Requesting a video list for {} videos', video_ids)
        paths = build_paths(['videos', video_ids],
                            custom_partial_paths if custom_partial_paths else VIDEO_LIST_PARTIAL_PATHS)
        path_response = self.nfsession.path_request(paths)
        return CustomVideoList(path_response)

    @cache_utils.cache_output(cache_utils.CACHE_COMMON, fixed_identifier='lolomo_category',
                              identify_append_from_kwarg_name='category_name', ignore_self_class=True)
    def req_lolomo_category(self, category_name):
        """Retrieve LoLoMo by category lists"""
        LOG.debug('Requesting LoLoMo "{}" category lists', category_name)
        try:
            return self._req_browser_lolomo_category(category_name)
        except req_exceptions.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in (404, 412):
                raise
            LOG.warn('Falling back to current LoCo rows for LoLoMo category after pathEvaluator {}', exc.response.status_code)
            return self._current_lolomo_category(category_name)
