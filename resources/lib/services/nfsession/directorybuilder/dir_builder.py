# -*- coding: utf-8 -*-
"""
    Copyright (C) 2017 Sebastian Golasch (plugin.video.netflix)
    Copyright (C) 2020 Stefano Gottardo (original implementation module)
    Prepare the data to build a directory of xbmcgui.ListItem's

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
import resources.lib.common as common
from resources.lib.utils.data_types import merge_data_type, CustomVideoList
from resources.lib.common.exceptions import CacheMiss, InvalidVideoListTypeError
from resources.lib.common import VideoId
from resources.lib.globals import G
from resources.lib.utils.api_paths import ART_SIZE_FHD, ART_SIZE_POSTER
from resources.lib.services.nfsession.directorybuilder.dir_builder_items \
    import (build_video_listing, build_subgenres_listing, build_season_listing, build_episode_listing,
            build_loco_listing, build_mainmenu_listing, build_profiles_listing, build_lolomo_category_listing)
from resources.lib.services.nfsession.directorybuilder.dir_path_requests import (DirectoryPathRequests,
                                                                                 _has_reference_entries,
                                                                                 metadata_with_title_page_fallback,
                                                                                 normalize_metadata_references)
from resources.lib.utils.logging import LOG, measure_exec_time_decorator


class DirectoryBuilder(DirectoryPathRequests):
    """Prepare the data to build a directory"""

    def __init__(self, nfsession):
        super().__init__(nfsession)
        # Slot allocation for IPC
        self.slots = [
            self.get_mainmenu,
            self.get_profiles,
            self.get_seasons,
            self.get_episodes,
            self.get_video_list,
            self.get_video_list_sorted,
            self.get_video_list_sorted_sp,
            self.get_category_list,
            self.get_video_list_supplemental,
            self.get_video_list_chunked,
            self.get_video_list_search,
            self.get_genres,
            self.get_subgenres,
            self.get_mylist_videoids_profile_switch,
            self.add_videoids_to_video_list_cache,
            self.get_continuewatching_videoid_exists
        ]

    @measure_exec_time_decorator(is_immediate=True)
    def get_mainmenu(self):
        loco_list = self.req_loco_list_root()
        return build_mainmenu_listing(loco_list)

    @measure_exec_time_decorator(is_immediate=True)
    def get_profiles(self, request_update, preselect_guid=None, detailed_info=True):
        """
        Get the list of profiles stored to the database
        :param request_update: when true, perform a request to the shakti API to fetch new profile data
        :param preselect_guid: if set the specified profile will be highlighted, else the current active profile
        """
        # The profiles data are automatically updated (parsed from falcorCache) in the following situations:
        # -At first log-in, see '_login' in nf_session_access.py
        # -When navigation accesses to the root path, see 'root' in directory.py (ref. to 'fetch_initial_page' call)
        if request_update:
            self.req_profiles_info()
        return build_profiles_listing(preselect_guid, detailed_info)

    @measure_exec_time_decorator(is_immediate=True)
    def get_seasons(self, pathitems, tvshowid_dict, perpetual_range_start):
        tvshowid = VideoId.from_dict(tvshowid_dict)
        season_list = self.req_seasons(tvshowid, perpetual_range_start=perpetual_range_start)
        return build_season_listing(season_list, tvshowid, pathitems)

    @measure_exec_time_decorator(is_immediate=True)
    def get_episodes(self, pathitems, seasonid_dict, perpetual_range_start):
        seasonid = VideoId.from_dict(seasonid_dict)
        episodes_list = self.req_episodes(seasonid, perpetual_range_start=perpetual_range_start)
        return build_episode_listing(episodes_list, seasonid, pathitems)

    @measure_exec_time_decorator(is_immediate=True)
    def get_video_list(self, list_id, menu_data, is_dynamic_id):
        menu_id = menu_data['path'][1]
        current_contexts = {
            'chosenForYou': ('windowedNewReleases',),
            'currentTitles': ('windowedNewReleases',),
            'mostViewed': ('mostWatched',)
        }
        if not is_dynamic_id and menu_id == 'continueWatching':
            video_list = self._browser_continue_watching_list()
        elif not is_dynamic_id and menu_id in current_contexts:
            video_list = self._video_list_from_lolomo_category_context(
                'comingSoon', current_contexts[menu_id], fallback_first=True)
        else:
            if not is_dynamic_id:
                list_id = self.get_loco_list_id_by_context(menu_data['loco_contexts'][0])
            # pylint: disable=unexpected-keyword-arg
            video_list = self.req_video_list(list_id, menu_data=menu_data, no_use_cache=menu_data.get('no_use_cache'))
        self._enrich_video_list_art(video_list)
        return build_video_listing(video_list, menu_data,
                                   mylist_items=self.req_mylist_items())

    @measure_exec_time_decorator(is_immediate=True)
    def get_video_list_sorted(self, pathitems, menu_data, sub_genre_id, perpetual_range_start, is_dynamic_id):
        context_id = None
        if is_dynamic_id and len(pathitems) > 2 and pathitems[2] != 'None':
            # Dynamic IDs for common video lists
            # The context_id can be:
            # -In the loco list: 'video list id'
            # -In the video list: 'sub-genre id'
            # -In the list of genres: 'sub-genre id'
            context_id = pathitems[2]
        if menu_data['path'][1] == 'recentlyAdded' and context_id:
            video_list = self._video_list_from_lolomo_category_context(
                'comingSoon', ('windowedNewReleases', 'newThisWeek', 'newOnNetflix', 'newOnNetflixThisWeek'),
                fallback_first=True)
            self._filter_unavailable_videos(video_list)
        else:
            # pylint: disable=unexpected-keyword-arg
            video_list = self.req_video_list_sorted(menu_data['request_context_name'],
                                                    context_id=context_id,
                                                    perpetual_range_start=perpetual_range_start,
                                                    menu_data=menu_data,
                                                    no_use_cache=menu_data.get('no_use_cache'))
        self._enrich_video_list_art(video_list)
        return build_video_listing(video_list, menu_data, sub_genre_id, pathitems, perpetual_range_start,
                                   self.req_mylist_items())

    def _enrich_video_list_art(self, video_list, include_refs=False):
        if not getattr(video_list, 'videos', None):
            return video_list
        for video in video_list.videos.values():
            if not isinstance(video, dict):
                continue
            needs_art = self._needs_metadata_boxart(video)
            needs_refs = include_refs and not _has_reference_entries(video, 'cast')
            if not needs_art and not needs_refs:
                continue
            try:
                videoid = VideoId.from_videolist_item(video)
            except Exception:  # pylint: disable=broad-except
                continue
            if videoid.mediatype not in (VideoId.MOVIE, VideoId.SHOW):
                continue
            try:
                metadata = self.nfsession._metadata(videoid)  # pylint: disable=protected-access
            except Exception as exc:  # pylint: disable=broad-except
                LOG.debug('Metadata enrichment skipped for {}: {}', videoid, exc)
                continue
            if needs_art:
                self._apply_metadata_art(video, metadata)
            if needs_refs:
                metadata = metadata_with_title_page_fallback(videoid.value, metadata)
                normalize_metadata_references(video_list.data, videoid.value, metadata, video)
        video_list.artitem = next(iter(video_list.videos.values()), None)
        return video_list

    @staticmethod
    def _needs_metadata_boxart(video):
        poster = common.get_path_safe(['boxarts', ART_SIZE_POSTER, 'jpg', 'value', 'url'], video)
        fallback = common.get_path_safe(['itemSummary', 'value', 'boxArt', 'url'], video)
        return not poster and not fallback

    @staticmethod
    def _apply_metadata_art(video, metadata):
        boxart = DirectoryBuilder._best_metadata_art(metadata, ('boxart', 'boxArt'), portrait=True)
        if boxart:
            video.setdefault('boxarts', {})[ART_SIZE_POSTER] = {'jpg': {'value': {'url': boxart}}}
        wide_art = DirectoryBuilder._best_metadata_art(metadata, ('artwork', 'interestingMoment'), portrait=False)
        if wide_art:
            video.setdefault('interestingMoment', {})[ART_SIZE_FHD] = {'jpg': {'value': {'url': wide_art}}}

    @staticmethod
    def _best_metadata_art(metadata, keys, portrait):
        candidates = []
        for key in keys:
            value = metadata.get(key) if isinstance(metadata, dict) else None
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = item.get('url')
                width = item.get('w') or item.get('width') or 0
                height = item.get('h') or item.get('height') or 0
                if not url or not width or not height:
                    continue
                if portrait and height <= width:
                    continue
                if not portrait and width <= height:
                    continue
                candidates.append((width * height, url))
        return max(candidates)[1] if candidates else ''

    def _filter_unavailable_videos(self, video_list):
        videos_type = type(video_list.videos)
        playable_videos = videos_type(
            (video_id, video)
            for video_id, video in video_list.videos.items()
            if video.get('availability', {}).get('value', {}).get('isPlayable', False))
        if len(playable_videos) == len(video_list.videos):
            return video_list
        video_list.videos = playable_videos
        video_list.artitem = next(iter(playable_videos.values()), None)
        video_list.contained_titles = [
            video.get('title', {}).get('value')
            for video in playable_videos.values()
            if video.get('title', {}).get('value')]
        return video_list

    def _video_list_from_lolomo_category_context(self, category_name, contexts, fallback_first=False):
        if isinstance(contexts, str):
            contexts = (contexts,)
        first_list_id = None
        for list_id, summary, video_list in self.req_lolomo_category(category_name).lists():
            if not first_list_id and video_list.videos:
                first_list_id = list_id
            if summary.get('context') in contexts:
                return self._browser_lolomo_video_list_by_id(category_name, list_id)
        if fallback_first and first_list_id:
            return self._browser_lolomo_video_list_by_id(category_name, first_list_id)
        raise InvalidVideoListTypeError(f'No LoLoMo category list with context {contexts} available')

    def _video_list_from_genre_context(self, genre_id, contexts):
        if isinstance(contexts, str):
            contexts = (contexts,)
        try:
            loco_list = self.req_loco_list_genre(genre_id)
            for list_id, video_list in loco_list.lists.items():
                if video_list.get('context') in contexts:
                    try:
                        return self._browser_genre_video_list_by_id(genre_id, list_id)
                    except Exception as exc:  # pylint: disable=broad-except
                        LOG.warn('Using materialized genre row {} after list lookup failed: {}', list_id, exc)
                        return video_list
        except Exception as exc:  # pylint: disable=broad-except
            LOG.warn('Continue Watching genre fallback failed: {}', exc)
        return CustomVideoList({'videos': {}})

    @measure_exec_time_decorator(is_immediate=True)
    def get_video_list_sorted_sp(self, path_items, menu_data, context_name, context_id, perpetual_range_start):
        # Method used for the menu search
        video_list = self.req_videos_list_sorted(context_name,
                                                 context_id=context_id,
                                                 perpetual_range_start=perpetual_range_start,
                                                 menu_data=menu_data)
        return build_video_listing(video_list, menu_data, None, path_items, perpetual_range_start,
                                   self.req_mylist_items())

    @measure_exec_time_decorator(is_immediate=True)
    def get_category_list(self, menu_data):
        lolomo_category_list = self.req_lolomo_category(menu_data['loco_contexts'][0])
        return build_lolomo_category_listing(lolomo_category_list, menu_data)

    @measure_exec_time_decorator(is_immediate=True)
    def get_video_list_supplemental(self, menu_data, video_id_dict, supplemental_type):
        video_list = self.req_video_list_supplemental(VideoId.from_dict(video_id_dict),
                                                      supplemental_type=supplemental_type)
        return build_video_listing(video_list, menu_data, mylist_items=[])

    @measure_exec_time_decorator(is_immediate=True)
    def get_video_list_chunked(self, pathitems, menu_data, chunked_video_list, perpetual_range_selector):
        video_list = self.req_video_list_chunked(chunked_video_list, perpetual_range_selector=perpetual_range_selector)
        return build_video_listing(video_list, menu_data, pathitems=pathitems, mylist_items=self.req_mylist_items())

    @measure_exec_time_decorator(is_immediate=True)
    def get_video_list_search(self, pathitems, menu_data, search_term, perpetual_range_start, path_params=None):
        video_list = self.req_video_list_search(search_term, perpetual_range_start=perpetual_range_start)
        self._enrich_video_list_art(video_list)
        return build_video_listing(video_list, menu_data,
                                   pathitems=pathitems, mylist_items=self.req_mylist_items(), path_params=path_params)

    @measure_exec_time_decorator(is_immediate=True)
    def get_genres(self, menu_data, genre_id, force_use_videolist_id):
        if genre_id:
            # Load the LoCo list of the specified genre
            loco_list = self.req_loco_list_genre(genre_id)
            if menu_data['path'][1] in ('tvshows', 'movies'):
                menu_data = dict(menu_data)
                menu_data['loco_contexts'] = None
                force_use_videolist_id = True
        elif menu_data['path'][1] == 'recommendations':
            return build_lolomo_category_listing(self.req_lolomo_category('comingSoon'), menu_data)
        else:
            # Load the LoCo root list filtered by 'loco_contexts' specified in the menu_data
            loco_list = self.req_loco_list_root()
        return build_loco_listing(loco_list, menu_data, force_use_videolist_id)

    @measure_exec_time_decorator(is_immediate=True)
    def get_subgenres(self, menu_data, genre_id):
        subgenre_list = self.req_subgenres(genre_id)
        return build_subgenres_listing(subgenre_list, menu_data)

    @measure_exec_time_decorator(is_immediate=True)
    def get_mylist_videoids_profile_switch(self):
        # Special method used for library sync with my list
        video_list = self.req_datatype_video_list_full('mylist', True)
        video_id_list = []
        video_id_list_type = []
        if video_list:
            for video_id, video in video_list.videos.items():
                video_id_list.append(video_id)
                video_id_list_type.append(video['summary']['value']['type'])
        return video_id_list, video_id_list_type

    @measure_exec_time_decorator(is_immediate=True)
    def add_videoids_to_video_list_cache(self, cache_bucket, cache_identifier, video_ids):
        """Add the specified video ids to a video list datatype in the cache (only if the cache item exists)"""
        try:
            video_list_sorted_data = G.CACHE.get(cache_bucket, cache_identifier)
            merge_data_type(video_list_sorted_data, self.req_datatype_video_list_byid(video_ids))
            G.CACHE.add(cache_bucket, cache_identifier, video_list_sorted_data)
        except CacheMiss:
            pass

    def get_continuewatching_videoid_exists(self, video_id):
        """
        Special method used to know if a video id exists in loco continue watching list

        :param video_id: videoid as [string] value
        :return: a tuple ([bool] true if videoid exists, [string] the current list id, that depends from loco id)
        """
        try:
            list_id = self.get_loco_list_id_by_context('continueWatching')
            video_list = self.req_video_list(list_id).videos if video_id else []
        except Exception as exc:  # pylint: disable=broad-except
            _ = exc # Silence IDE warning, code seems to intentionally ignoring any exception
            current_list = self._video_list_from_genre_context('1592210', ('continueWatching',))
            list_id = current_list.videoid.value if getattr(current_list, 'videoid', None) else None
            video_list = current_list.videos if video_id else []
        return video_id in video_list, list_id
