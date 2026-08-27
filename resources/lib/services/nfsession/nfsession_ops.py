# -*- coding: utf-8 -*-
"""
    Copyright (C) 2017 Sebastian Golasch (plugin.video.netflix)
    Copyright (C) 2020 Stefano Gottardo (original implementation module)
    Provides methods to perform operations within the Netflix session

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
import json
import time
from datetime import datetime, timedelta

import requests
import requests.exceptions as req_exceptions
import xbmc

import resources.lib.common as common
import resources.lib.utils.website as website
from resources.lib.common import cache_utils
from resources.lib.common.exceptions import (NotLoggedInError, MissingCredentialsError, WebsiteParsingError,
                                             MbrStatusAnonymousError, MetadataNotAvailable, LoginValidateError,
                                             InvalidProfilesError, ErrorMsgNoReport, CacheMiss, APIError)
from resources.lib.globals import G
from resources.lib.kodi import ui
from resources.lib.services.nfsession.directorybuilder.dir_path_requests import (_metadata_image_url,
                                                                                _metadata_trailer_url,
                                                                                metadata_with_title_page_fallback,
                                                                                normalize_metadata_references)
from resources.lib.database.db_utils import TABLE_SESSION
from resources.lib.services.nfsession.session.access import (BROWSER_HEADERS, MFA_NEXT_NODE_ID,
                                                            clcs_context_headers)
from resources.lib.services.nfsession.session.endpoints import BASE_URL
from resources.lib.services.nfsession.session.http_requests import ACCOUNT_GRAPHQL_URL
from resources.lib.services.nfsession.session.path_requests import SessionPathRequests
from resources.lib.utils import cookies
from resources.lib.utils.api_paths import (EPISODES_PARTIAL_PATHS, ART_PARTIAL_PATHS, build_paths,
                                           VIDEO_LIST_PARTIAL_PATHS, ART_SIZE_FHD, ART_SIZE_POSTER)
from resources.lib.utils.logging import LOG, measure_exec_time_decorator


def _is_playable_direct_trailer_url(trailer_url):
    """Require the direct fallback to resolve to actual video content."""
    if not isinstance(trailer_url, str) or not trailer_url.startswith('http'):
        return False
    try:
        response = requests.get(
            trailer_url,
            headers={
                'Range': 'bytes=0-0',
                'User-Agent': common.get_user_agent(enable_android_mediaflag_fix=True)
            },
            stream=True,
            timeout=(2, 4))
        try:
            content_type = (response.headers.get('content-type') or '').lower()
            return response.status_code in (200, 206) and content_type.startswith('video/')
        finally:
            response.close()
    except req_exceptions.RequestException:
        return False


GRAPHQL_OP_SET_PROFILE_PIN = 'd1528f2f-ed01-4dc1-b870-ee91bb2c3850'
GRAPHQL_OP_PROFILE_LOCK_TEMPLATE = 'd821963d-5bb5-43c7-a2d1-2cf7eab9f44e'
GRAPHQL_OP_REMOVE_PROFILE_PIN = 'f490f470-b47c-4788-98e0-b13dac06f611'


class NFSessionOperations(SessionPathRequests):
    """Provides methods to perform operations within the Netflix session"""

    def __init__(self):
        super().__init__()
        # Slot allocation for IPC
        self.slots = [
            self.get_safe,
            self.post_safe,
            self.post_graphql,
            self.login,
            self.login_auth_data,
            self.logout,
            self.path_request,
            self.perpetual_path_request,
            self.callpath_request,
            self.fetch_initial_page,
            self.refresh_session_data,
            self.activate_profile,
            self.parental_control_data,
            self.set_parental_control_data,
            self.set_profile_lock,
            self.get_metadata,
            self.get_videoid_info,
            self.get_direct_trailer
        ]
        # Share the activate profile function to SessionBase class
        self.external_func_activate_profile = self.activate_profile
        self.dt_initial_page_prefetch = None
        # Try prefetch login
        if self.prefetch_login():
            try:
                # Try prefetch initial page
                response = self.get_safe('browse')
                api_data = website.extract_session_data(response, update_profiles=True)
                self.auth_url = api_data['auth_url']
                self.dt_initial_page_prefetch = datetime.now()
            except Exception as exc:  # pylint: disable=broad-except
                LOG.warn('Prefetch initial page failed: {}', exc)

    @measure_exec_time_decorator(is_immediate=True)
    def fetch_initial_page(self):
        """Fetch initial page"""
        # It is mandatory fetch initial page data at every add-on startup to prevent/check possible side effects:
        # - Check if the account subscription is regular
        # - Avoid TooManyRedirects error, can happen when the profile used in nf session actually no longer exists
        # - Refresh the session data
        # - Update the profiles (and sanitize related features) without submitting another request
        if self.dt_initial_page_prefetch and datetime.now() <= self.dt_initial_page_prefetch + timedelta(minutes=30):
            # We do not know if/when the user will open the add-on, some users leave the device turned on more than 24h
            # then we limit the prefetch validity to 30 minutes
            self.dt_initial_page_prefetch = None
            return
        LOG.debug('Fetch initial page')
        from requests import exceptions
        try:
            self.refresh_session_data(True)
        except exceptions.TooManyRedirects:
            # This error can happen when the profile used in nf session actually no longer exists,
            # something wrong happen in the session then the server try redirect to the login page without success.
            # (CastagnaIT: i don't know the best way to handle this borderline case, but login again works)
            self.session.cookies.clear()
            self.login()

    def refresh_session_data(self, update_profiles):
        response = self.get_safe('browse')
        api_data = self.website_extract_session_data(response, update_profiles=update_profiles)
        self.auth_url = api_data['auth_url']

    @measure_exec_time_decorator(is_immediate=True)
    def activate_profile(self, guid):
        """Set the profile identified by guid as active"""
        LOG.debug('Switching to profile {}', guid)
        if guid == G.LOCAL_DB.get_active_profile_guid():
            LOG.info('The profile guid {} is already set, activation not needed.', guid)
            return
        if xbmc.Player().isPlayingVideo():
            # Change the current profile while a video is playing can cause problems with outgoing HTTP requests
            # (MSL/NFSession) causing a failure in the HTTP request or sending data on the wrong profile
            raise ErrorMsgNoReport('It is not possible select a profile while a video is playing.')
        LOG.info('Activating profile {}', guid)
        try:
            self._switch_profile_request(guid)
            # Fetch browse page to get a fresh authURL for the new profile
            response = self.get_safe('browse')
            self.auth_url = website.extract_session_data(response)['auth_url']
        except Exception as exc:
            raise InvalidProfilesError('Unable to access to the selected profile.') from exc
        G.LOCAL_DB.switch_active_profile(guid)
        G.CACHE_MANAGEMENT.identifier_prefix = guid
        cookies.save(self.session.cookies)

    def _switch_profile_request(self, guid):
        """Switch the active profile server-side, with the previous address as fallback"""
        from requests import exceptions
        try:
            self.get_safe('switch_profile',
                          params={'switchProfileGuid': guid, '_': int(time.time() * 1000)})
            return
        except exceptions.HTTPError as exc:
            if getattr(exc.response, 'status_code', None) not in (400, 401, 403, 404, 410):
                raise
            LOG.warn('Profile switch with the member api returned {}, using the previous address',
                     exc.response.status_code)
        self.get_safe('switch_profile_legacy', params={'tkn': guid})

    def parental_control_data(self, guid, password):  # pylint: disable=unused-argument
        # Warning - parental control levels vary by country or region, no fixed values can be used
        # Note: The language of descriptions change in base of the language of selected profile
        response = self._restrictions_page(guid)
        if '/mfa' in response.url:
            # Netflix asks to confirm the identity with the code that it sends by e-mail,
            # the page reached by the redirect is the one that opens the check
            LOG.debug('The restrictions page asks to verify the identity, starting the check')
            self.verify_identity_mfa(guid, response)
            response = self._restrictions_page(guid, from_identity_check=True)
            LOG.info('MFA: after the check the restrictions page landed on {} through {}',
                     response.url,
                     [(r.status_code, r.headers.get('Location', '')[:90]) for r in response.history])
            self.flush_mfa_feedback()
            if '/mfa' in response.url:
                # The website closes the check with the feedback, ask the page once more
                response = self._restrictions_page(guid, from_identity_check=True)
                LOG.info('MFA: after the closing feedback the page landed on {}', response.url)
            if '/mfa' in response.url:
                raise ErrorMsgNoReport('Netflix did not accept the identity check, '
                                       'the settings cannot be changed.')
        extracted_content = website.extract_parental_control_page_data(response.text, guid)
        LOG.info('PARENTAL: {} levels available, the profile is on {}',
                 len(extracted_content['rating_levels']),
                 extracted_content['data']['maturity'])
        extracted_content['data']['token'] = website.extract_auth_url(response.text)
        return extracted_content

    def set_profile_lock(self, guid, pin):
        """Set or remove the PIN that locks a profile"""
        # Netflix names the two actions apart, and tells with the first call whether the
        # identity has to be confirmed again or the change can be made straight away
        action = 'EDIT_PROFILE_LOCK' if pin else 'DELETE_PROFILE_LOCK'
        node = self._mfa_next_node(guid, action)
        LOG.info('PROFILE LOCK: {} continues on {}', action, node)
        if node == 'MFA_SELECT_FACTOR':
            self.verify_identity_mfa_embedded(guid, 'MANAGE_PROFILE_LOCK', action)
            # The website loads the page the check leads to and closes it with the feedback,
            # the step is not finished until both are done
            LOG.info('PROFILE LOCK: loading the page the check leads to')
            pin_url = f'{BASE_URL}/settings/lock/pinentry/{guid}'
            self.post_graphql('ProfileLockTemplate', {}, GRAPHQL_OP_PROFILE_LOCK_TEMPLATE,
                              pin_url, ACCOUNT_GRAPHQL_URL,
                              clcs_context_headers('ProfileLockTemplate', pin_url))
            self.flush_mfa_feedback()
            LOG.info('PROFILE LOCK: the check was closed, asking again')
            node = self._mfa_next_node(guid, action)
            LOG.info('PROFILE LOCK: after the check {} continues on {}', action, node)
            if node == 'MFA_SELECT_FACTOR':
                raise ErrorMsgNoReport('Netflix keeps asking to confirm the identity, '
                                       'the profile lock cannot be changed.')
        return self._post_profile_lock(guid, pin)

    def _mfa_next_node(self, guid, growth_action):  # pylint: disable=unused-argument
        """Ask Netflix what the next step of the journey is, it says if the identity is needed"""
        page_url = f'{BASE_URL}/settings/lock/{guid}'
        data = self.post_graphql('GrowthGetNextNodeForMfaFlow',
                                 {'currentNode': 'MANAGE_PROFILE_LOCK',
                                  'growthActionName': growth_action,
                                  'sessionId': ''},
                                 MFA_NEXT_NODE_ID, page_url, ACCOUNT_GRAPHQL_URL,
                                 clcs_context_headers('GrowthGetNextNodeForMfaFlow', page_url))
        return common.get_path_safe(
            ['data', 'growthGetNextNodeForMfaFlow', 'userJourneyNodeName'], data, False, '')

    def _post_profile_lock(self, guid, pin):
        referer = f'{BASE_URL}/settings/lock/{guid}'
        if pin:
            data = self.post_graphql('UpdateProfileAccessPin',
                                     {'profileGuid': guid,
                                      'profilePin': str(pin),
                                      'requirePinToCreateProfiles': False},
                                     GRAPHQL_OP_SET_PROFILE_PIN, referer, ACCOUNT_GRAPHQL_URL,
                                     clcs_context_headers('UpdateProfileAccessPin', referer))
            result = common.get_path_safe(['data', 'growthSetProfilePin'], data, False, None)
        else:
            data = self.post_graphql('RemoveProfileAccessPin',
                                     {'profileGuid': guid},
                                     GRAPHQL_OP_REMOVE_PROFILE_PIN, referer, ACCOUNT_GRAPHQL_URL,
                                     clcs_context_headers('RemoveProfileAccessPin', referer))
            result = common.get_path_safe(['data', 'growthRemoveProfilePin'], data, False, None)
        if not result:
            LOG.warn('The profile lock was refused by Netflix: {}', data)
            raise APIError('Netflix did not accept the change of the profile lock.')
        LOG.info('PROFILE LOCK: the profile {} is now {}', guid, 'locked' if pin else 'unlocked')

    def _restrictions_page(self, guid, from_identity_check=False):
        # After the check the website reaches the page from the check page, keep the same referer
        referer = (f'{BASE_URL}/mfa?guid={guid}' if from_identity_check
                   else f'{BASE_URL}/account/profiles')
        # Netflix ties the identity check to the browser that passed it, the page has to be
        # asked with the same identity used by the check, not with the one of the add-on
        headers = dict(BROWSER_HEADERS)
        headers.update({'Referer': referer,
                        'Sec-Fetch-Dest': 'document',
                        'Sec-Fetch-Mode': 'navigate',
                        'Sec-Fetch-Site': 'same-origin',
                        'Sec-Fetch-User': '?1',
                        'Upgrade-Insecure-Requests': '1'})
        response = self.session.get(f'{BASE_URL}/settings/restrictions/{guid}',
                                    headers=headers, allow_redirects=True, timeout=10)
        response.raise_for_status()
        return response

    def set_parental_control_data(self, data):
        """Save the maturity level of a profile"""
        params = {
            'landingURL': f'/settings/restrictions/{data["guid"]}',
            'landingOrigin': BASE_URL,
            'inapp': 'false',
            'languages': G.LOCAL_DB.get_profile_config('language', 'en-US') or 'en-US',
            'netflixClientPlatform': 'browser',
            'method': 'call',
            'callPath': '["aui","moneyball","next"]',
            'falcor_server': '0.1.0'
        }
        payload = {
            'flow': 'websiteMember',
            'mode': 'maturityRating',
            'action': 'saveAction',
            'fields': {
                'experience': {'value': data['experience']},
                'maturity': {'value': int(data['maturity'])},
                'profileGuid': {'value': data['guid']}
            }
        }
        response = self.session.post(
            f'{BASE_URL}/api/aui/pathEvaluator/web/%5E2.0.0',
            params=params,
            data={'authURL': data['token'],
                  'tracingId': G.LOCAL_DB.get_value('ui_version', '', table=TABLE_SESSION),
                  'tracingGroupId': 'www.netflix.com',
                  'param': json.dumps(payload, separators=(',', ':'))},
            headers={'Accept': '*/*',
                     'Content-Type': 'application/x-www-form-urlencoded',
                     'Origin': BASE_URL,
                     'Referer': f'{BASE_URL}/settings/restrictions/{data["guid"]}',
                     # Without the routing header the address is not served
                     'x-netflix.request.routing':
                         '{"path":"/nq/aui/endpoint/%5E1.0.0-web/pathEvaluator",'
                         '"control_tag":"auinqweb"}',
                     'x-netflix.nq.stack': 'prod',
                     'x-netflix.client.request.name': 'ui/xhrUnclassified',
                     'x-netflix.request.attempt': '1',
                     'x-netflix.request.client.context': '{"appstate":"foreground"}',
                     'X-Netflix.uiVersion': G.LOCAL_DB.get_value('ui_version', '', table=TABLE_SESSION),
                     'X-Netflix.clientType': 'akira',
                     'Sec-Fetch-Dest': 'empty',
                     'Sec-Fetch-Mode': 'cors',
                     'Sec-Fetch-Site': 'same-origin'},
            timeout=10)
        response.raise_for_status()
        result = common.get_path_safe(
            ['jsonGraph', 'aui', 'moneyball', 'next', 'value', 'result', 'fields', 'result', 'value'],
            response.json(), False, False)
        if not result:
            LOG.warn('The maturity level was refused by Netflix: {}', response.text[:300])
            raise ErrorMsgNoReport('Netflix did not accept the new maturity level.')
        LOG.info('PARENTAL: the maturity level of the profile {} is now {}',
                 data['guid'], data['maturity'])

    def website_extract_session_data(self, content, **kwargs):
        """Extract session data and handle errors"""
        try:
            return website.extract_session_data(content, **kwargs)
        except WebsiteParsingError as exc:
            LOG.error('An error occurs in extract session data: {}', exc)
            raise
        except (LoginValidateError, MbrStatusAnonymousError) as exc:
            LOG.warn('The session data is not more valid ({})', type(exc).__name__)
            common.purge_credentials()
            self.session.cookies.clear()
            # Clear the user ID tokens are tied to the credentials
            self.msl_handler.clear_user_id_tokens()
            raise NotLoggedInError from exc

    @measure_exec_time_decorator(is_immediate=True)
    def get_metadata(self, videoid, refresh=False):
        """Retrieve additional metadata for the given VideoId"""
        # Get the parent VideoId (when the 'videoid' is a type of EPISODE/SEASON)
        parent_videoid = videoid.derive_parent(common.VideoId.SHOW)
        # Delete the cache if we need to refresh the all metadata
        if refresh:
            G.CACHE.delete(cache_utils.CACHE_METADATA, str(parent_videoid))
        if videoid.mediatype == common.VideoId.EPISODE:
            try:
                metadata_data = self._episode_metadata(videoid, parent_videoid)
            except KeyError as exc:
                # The episode metadata not exist (case of new episode and cached data outdated)
                # In this case, delete the cache entry and try again safely
                LOG.debug('find_episode_metadata raised an error: {}, refreshing cache', exc)
                try:
                    metadata_data = self._episode_metadata(videoid, parent_videoid, refresh_cache=True)
                except KeyError as exc_:
                    # The new metadata does not contain the episode
                    LOG.error('Episode metadata not found, find_episode_metadata raised an error: {}', exc_)
                    raise MetadataNotAvailable from exc_
        else:
            metadata_data = self._metadata(video_id=parent_videoid), None
        return metadata_data

    def get_direct_trailer(self, videoid):
        """Return a fresh, verified public trailer fallback for a title."""
        cache_identifier = f'direct_trailer_{videoid}'
        try:
            return G.CACHE.get(cache_utils.CACHE_SUPPLEMENTAL, cache_identifier)
        except CacheMiss:
            pass
        try:
            metadata_data = self.get_safe(
                endpoint='metadata',
                params={'movieid': videoid.value, '_': int(time.time() * 1000)})
            metadata_video = metadata_with_title_page_fallback(
                videoid.value, metadata_data.get('video') or {})
        except (MetadataNotAvailable, AttributeError, KeyError, TypeError, req_exceptions.RequestException):
            result = {}
            G.CACHE.add(cache_utils.CACHE_SUPPLEMENTAL, cache_identifier, result)
            return result
        trailer_url = _metadata_trailer_url(metadata_video)
        if not _is_playable_direct_trailer_url(trailer_url):
            result = {}
            G.CACHE.add(cache_utils.CACHE_SUPPLEMENTAL, cache_identifier, result)
            return result
        trailer_data = metadata_video.get('trailer') or {}
        trailer_title = trailer_data.get('name') if isinstance(trailer_data, dict) else ''
        poster = _metadata_image_url(
            metadata_video, ('boxart', 'boxArt', 'boxarts'), portrait=True)
        result = {
            'url': trailer_url,
            'title': trailer_title or metadata_video.get('title') or '',
            'synopsis': metadata_video.get('synopsis') or metadata_video.get('regularSynopsis') or '',
            'year': metadata_video.get('year') or metadata_video.get('releaseYear') or 0,
            'poster': poster
        }
        G.CACHE.add(cache_utils.CACHE_SUPPLEMENTAL, cache_identifier, result)
        return result

    def _episode_metadata(self, episode_videoid, tvshow_videoid, refresh_cache=False):
        if refresh_cache:
            G.CACHE.delete(cache_utils.CACHE_METADATA, str(tvshow_videoid))
        show_metadata = self._metadata(video_id=tvshow_videoid)
        episode_metadata, season_metadata = common.find_episode_metadata(episode_videoid, show_metadata)
        return episode_metadata, season_metadata, show_metadata

    @cache_utils.cache_output(cache_utils.CACHE_METADATA, identify_from_kwarg_name='video_id', ignore_self_class=True)
    def _metadata(self, video_id):
        """Retrieve additional metadata for a video.
        This is a separate method from get_metadata() to work around caching issues
        when new episodes are added to a tv show by Netflix."""
        LOG.debug('Requesting metadata for {}', video_id)
        metadata_data = self.get_safe(endpoint='metadata',
                                      params={'movieid': video_id.value,
                                              '_': int(time.time() * 1000)})
        if not metadata_data:
            # This return empty
            # - if the metadata is no longer available
            # - if it has been exported a tv show/movie from a specific language profile that is not
            #   available using profiles with other languages
            raise MetadataNotAvailable
        return metadata_data['video']

    def update_loco_context(self, loco_root_id, list_context_name, list_id, list_index):
        """Update a loco list by context"""
        path = [['locos', loco_root_id, 'refreshListByContext']]
        # After the introduction of LoCo, the following notes are to be reviewed (refers to old LoLoMo):
        #   The fourth parameter is like a request-id, but it does not seem to match to
        #   serverDefs/date/requestId of reactContext nor to request_id of the video event request,
        #   seem to have some kind of relationship with renoMessageId suspect with the logblob but i am not sure.
        #   I noticed also that this request can also be made with the fourth parameter empty.
        #   The fifth parameter unknown
        params = [list_id,
                  int(list_index),
                  list_context_name,
                  '',
                  '']
        # path_suffixs = [
        #    [{'from': 0, 'to': 100}, 'itemSummary'],
        #    [['componentSummary']]
        # ]
        try:
            response = self.callpath_request(path, params)
            LOG.debug('refreshListByContext response: {}', response)
            # The call response return the new context id of the previous invalidated loco context_id
            # and if path_suffixs is added return also the new video list data
        except Exception as exc:  # pylint: disable=broad-except
            LOG.warn('refreshListByContext failed: {}', exc)
            if not LOG.is_enabled:
                return
            ui.show_notification(title=common.get_local_string(30105),
                                 msg='An error prevented the update the loco context on Netflix',
                                 time=10000)

    def get_videoid_info(self, videoid):
        """Get infolabels and arts from cache (if exist) otherwise will be requested"""
        from resources.lib.kodi.infolabels import get_info, get_art
        profile_language_code = G.LOCAL_DB.get_profile_config('language', '')
        try:
            infos = get_info(videoid, None, None, profile_language_code)[0]
            art = get_art(videoid, None, profile_language_code)
            if infos.get('Cast') and (videoid.mediatype == common.VideoId.EPISODE or infos.get('Trailer')):
                return infos, art
            LOG.debug('Cached video info for {} is missing cast/trailer; refreshing metadata', videoid)
        except (AttributeError, TypeError):
            pass
        if videoid.mediatype == common.VideoId.EPISODE:
            paths = (build_paths(['videos', int(videoid.value)], EPISODES_PARTIAL_PATHS) +
                     build_paths(['videos', int(videoid.tvshowid)],
                                 ART_PARTIAL_PATHS + [[['title', 'delivery']]]))
        else:
            paths = build_paths(['videos', int(videoid.value)], VIDEO_LIST_PARTIAL_PATHS)
        try:
            raw_data = self.path_request(paths)
        except req_exceptions.HTTPError as exc:
            LOG.warn('Video info pathEvaluator lookup failed: {}. Falling back to metadata endpoint.', exc)
            raw_data = self._get_videoid_info_metadata(videoid)
        infos = get_info(videoid, raw_data['videos'][videoid.value], raw_data, profile_language_code)[0]
        if (videoid.mediatype != common.VideoId.EPISODE and
                (not infos.get('Cast') or not infos.get('Trailer'))):
            LOG.debug('Video info for {} is missing cast/trailer; refreshing from metadata endpoint', videoid)
            raw_data = self._get_videoid_info_metadata(videoid)
            infos = get_info(videoid, raw_data['videos'][videoid.value], raw_data, profile_language_code)[0]
        art = get_art(videoid, raw_data['videos'][videoid.value], profile_language_code)
        return infos, art

    def _get_videoid_info_metadata(self, videoid):
        metadata_data = self.get_safe(
            endpoint='metadata',
            params={'movieid': videoid.value, '_': int(time.time() * 1000)})
        video = metadata_with_title_page_fallback(videoid.value, metadata_data['video'])
        item = self._metadata_video_to_path_item(videoid, video)
        videos = {videoid.value: item}
        raw_data = {'videos': videos}
        normalize_metadata_references(raw_data, videoid.value, video, item)
        if videoid.mediatype == common.VideoId.EPISODE and videoid.tvshowid:
            videos.setdefault(videoid.tvshowid, {
                'title': {'value': video.get('seriesTitle') or video.get('showTitle') or ''},
                'delivery': {'value': {}}
            })
        return raw_data

    @staticmethod
    def _metadata_video_to_path_item(videoid, video):
        title = video.get('title') or str(videoid.value)
        synopsis = video.get('synopsis') or video.get('regularSynopsis') or ''
        boxart_url = NFSessionOperations._find_metadata_image_url(video, ('boxArt', 'boxart', 'artwork'))
        still_url = NFSessionOperations._find_metadata_image_url(video, ('interestingMoment', 'interestingMomentUrl'))
        item = {
            'summary': {'value': {
                'id': int(videoid.value),
                'type': videoid.mediatype,
                'name': title
            }},
            'title': {'value': title},
            'synopsis': {'value': synopsis},
            'regularSynopsis': {'value': synopsis},
            'runtime': {'value': video.get('runtime') or 0},
            'releaseYear': {'value': video.get('year') or video.get('releaseYear') or 0},
            'delivery': {'value': video.get('delivery') or {}},
            'availability': {'value': {'isPlayable': True}},
            'queue': {'value': {'inQueue': False}},
            'inRemindMeList': {'value': False},
            'bookmarkPosition': {'value': (video.get('bookmark') or {}).get('offset', 0)},
            'creditsOffset': {'value': video.get('creditsOffset') or 0},
            'watchedToEndOffset': {'value': video.get('watchedToEndOffset') or 0},
            'watched': {'value': bool((video.get('bookmark') or {}).get('watchedDate'))},
            'trackIds': {'value': {}},
            'requestId': {'value': ''}
        }
        if boxart_url:
            art_value = {'url': boxart_url}
            item['boxarts'] = {ART_SIZE_POSTER: {'jpg': {'value': art_value}}}
            item['itemSummary'] = {'value': {'id': int(videoid.value), 'title': title, 'boxArt': {'url': boxart_url}}}
        if still_url:
            item['interestingMoment'] = {ART_SIZE_FHD: {'jpg': {'value': {'url': still_url}}}}
        return item

    @staticmethod
    def _find_metadata_image_url(video, keys):
        for key in keys:
            value = video.get(key)
            if isinstance(value, str) and value.startswith('http'):
                return value
            if isinstance(value, dict):
                url = NFSessionOperations._find_url_in_dict(value)
                if url:
                    return url
        return ''

    @staticmethod
    def _find_url_in_dict(data):
        for value in data.values():
            if isinstance(value, str) and value.startswith('http'):
                return value
            if isinstance(value, dict):
                url = NFSessionOperations._find_url_in_dict(value)
                if url:
                    return url
        return ''

    def get_loco_data(self):
        """
        Get the LoCo root id and the continueWatching list data references
        needed for events requests and update_loco_context
        """
        # This data will be different for every profile,
        #  while the loco root id should be a fixed value (expiry?), the 'continueWatching' context data
        #  will change every time that nfsession update_loco_context is called
        context_name = 'continueWatching'
        loco_data = self.path_request([['loco', [context_name], ['context', 'id', 'index']]])
        loco_root = loco_data['loco']['value'][1]
        _loco_data = {'root_id': loco_root}
        # 22/11/2021 With some users the API path request not provide the "locos" data
        if 'locos' in loco_data and context_name in loco_data['locos'][loco_root]:
            # NOTE: In the new profiles, there is no 'continueWatching' list and no data will be provided
            _loco_data['list_context_name'] = context_name
            _loco_data['list_index'] = loco_data['locos'][loco_root][context_name]['value'][2]
            _loco_data['list_id'] = loco_data['locos'][loco_root][_loco_data['list_index']]['value'][1]
        return _loco_data
