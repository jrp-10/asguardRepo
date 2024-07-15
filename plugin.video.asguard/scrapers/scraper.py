"""
    Asguard Addon
    Copyright (C) 2024 MrBlamo

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
import abc
import datetime
import gzip
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from io import StringIO
import requests
from bs4 import BeautifulSoup
from asguard_lib import control

from asguard_lib import cloudflare
from asguard_lib import cf_captcha
import kodi
import log_utils  # @UnusedImport

from asguard_lib import scraper_utils
from asguard_lib.constants import FORCE_NO_MATCH, Q_ORDER, SHORT_MONS, VIDEO_TYPES, DEFAULT_TIMEOUT
from asguard_lib.db_utils import DB_Connection
from asguard_lib.utils2 import i18n, ungz

import xbmcgui

try:
    import resolveurl
except ImportError:
    kodi.notify(msg=i18n('smu_failed'), duration=5000)

logger = log_utils.Logger.get_logger()

BASE_URL = ''
CAPTCHA_BASE_URL = 'https://www.google.com/recaptcha/api'
FLARESOLVERR_URL = 'http://localhost:8191'
COOKIEPATH = kodi.translate_path(kodi.get_profile())
MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
MAX_RESPONSE = 1024 * 1024 * 10
CF_CAPCHA_ENABLED = kodi.get_setting('cf_captcha') == 'true'

class ScrapeError(Exception):
    pass

class NoRedirection(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):  # @UnusedVariable
        logger.log('Stopping Redirect', log_utils.LOGDEBUG)
        return response

    https_response = http_response

abstractstaticmethod = abc.abstractmethod
class abstractclassmethod(classmethod):

    __isabstractmethod__ = True

    def __init__(self, callable):
        callable.__isabstractmethod__ = True
        super(abstractclassmethod, self).__init__(callable)


class Scraper(object):
    __metaclass__ = abc.ABCMeta
    base_url = BASE_URL
    __db_connection = None
    worker_id = None
    debrid_resolvers = resolveurl
    row_pattern = r'\s*<a\s+href="(?P<link>[^"]+)">(?P<title>[^<]+)</a>\s+(?P<date>\d+-[a-zA-Z]+-\d+ \d+:\d+)\s+(?P<size>-|\d+)'
    scrapers = []

    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout

    @abstractclassmethod
    def provides(cls):
        """
        Must return a list/set/frozenset of VIDEO_TYPES that are supported by this scraper. Is a class method so that instances of the class
        don't have to be instantiated to determine they are not useful

        * Datatypes set or frozenset are preferred as existence checking is faster with sets
        """
        raise NotImplementedError

    @classmethod
    def get_name(cls):
        """
        Must return a string that is a name that will be used through out the UI and DB to refer to urls from this source
        Should be descriptive enough to be recognized but short enough to be presented in the UI
        """
        raise NotImplementedError
    
    @classmethod
    def register_scraper(cls, scraper):
        """
        Register a new scraper class.
        """
        cls.scrapers.append(scraper)

    @classmethod
    def get_scraper_names(cls):
        """
        Get the names of all registered scrapers.
        """
        return [scraper.get_name() for scraper in cls.scrapers]

    def resolve_link(self, link):
        """
        Must return a string that is a resolveurl resolvable link given a link that this scraper supports

        link: a url fragment associated with this site that can be resolved to a hoster link

        * The purpose is many streaming sites provide the actual hoster link in a separate page from link
        on the video page.
        * This method is called for the user selected source before calling resolveurl on it.
        """
        if link.startswith('magnet:') or link.endswith('.torrent') or link.startswith('http') or link.startswith('https') or link.startswith('ftp'):
            return link
        elif not link.startswith('http') or link.startswith('ftp'):
            return scraper_utils.urljoin(self.base_url, link)
        elif link.startswith('http'):
            return link
        else:
            return link


    def format_source_label(self, item):
        """
        Must return a string that is to be the label to be used for this source in the "Choose Source" dialog.

        Args:
            item (dict): One element of the list that is returned from get_sources for this scraper.

        Returns:
            str: Formatted label for the source.
        """
        label_parts = [f"[{item['quality']}"]

        if 'label' in item:
            label_parts.append(f"({item['label']})")

        if item.get('torrent'):
            label_parts.append("(Torrent)")

        if item.get('4K'):
            label_parts.append("(HD4K)")

        if item.get('3D'):
            label_parts.append("(3D)")

        if 'format' in item:
            label_parts.append(f"({item['format']})")

        if 'version' in item:
            label_parts.append(item['version'])

        label_parts.append(item['host'])

        if 'views' in item and item['views'] is not None:
            label_parts.append(f"({item['views']} views)")

        if 'rating' in item and item['rating'] is not None:
            label_parts.append(f"({item['rating']}/100)")

        if 'size' in item:
            label_parts.append(f"({item['size']})")

        if item.get('subs'):
            label_parts.append(f"({item['subs']})")

        if 'extra' in item:
            label_parts.append(f"[{item['extra']}]")

        if 'seeders' in item:
            label_parts.append(f"({item['seeders']} seeders)")

        return ' '.join(label_parts)


    @abc.abstractmethod
    def get_sources(self, video):
        """
        Must return a list of dictionaries that are potential link to hoster sites (or links to links to hoster sites)
        Each dictionary must contain elements of at least:
            * multi-part: True if this source is one part of a whole
            * class: a reference to an instance of the scraper itself
            * host: the hostname of the hoster
            * url: the url that is a link to a hoster, or a link to a page that this scraper can resolve to a link to a hoster
            * quality: one of the QUALITIES values, or None if unknown; users can sort sources by quality
            * views: count of the views from the site for this source or None is unknown; Users can sort sources by views
            * rating: a value between 0 and 100; 0 being worst, 100 the best, or None if unknown. Users can sort sources by rating.
            * direct: True if url is a direct link to a media file; False if not. If not present; assumption is direct
            * other keys are allowed as needed if they would be useful (e.g. for format_source_label)

        video is an object of type ScraperVideo:
            video_type: one of VIDEO_TYPES for whatever the sources should be for
            title: the title of the tv show or movie
            year: the year of the tv show or movie
            season: only present for tv shows; the season number of the video for which sources are requested
            episode: only present for tv shows; the episode number of the video for which sources are requested
            ep_title: only present for tv shows; the episode title if available
        """
        raise NotImplementedError

    def get_url(self, video):
        """
        Must return a url for the site this scraper is associated with that is related to this video.

        video is an object of type ScraperVideo:
            video_type: one of VIDEO_TYPES this url is for (e.g. EPISODE urls might be different than TVSHOW urls)
            title: the title of the tv show or movie
            year: the year of the tv show or movie
            season: only present for season or episode VIDEO_TYPES; the season number for the url being requested
            episode: only present for season or episode VIDEO_TYPES; the episode number for the url being requested
            ep_title: only present for tv shows; the episode title if available

        * Generally speaking, domain should not be included
        """
        return self._default_get_url(video)

    @abc.abstractmethod
    def search(self, video_type, title, year, season=''):
        """
        Must return a list of results returned from the site associated with this scraper when doing a search using the input parameters

        If it does return results, it must be a list of dictionaries. Each dictionary must contain at least the following:
            * title: title of the result
            * year: year of the result
            * url: a url fragment that is the url on the site associated with this scraper for this season result item

        video_type: one of the VIDEO_TYPES being searched for. Only tvshows and movies are expected generally
        title: the title being search for
        year: the year being search for
        season: the season being searched for (only required if video_type == VIDEO_TYPES.SEASON)

        * Method must be provided, but can raise NotImplementedError if search not available on the site
        """
        raise NotImplementedError

    @classmethod
    def get_settings(cls):
        """
        Returns a list of settings to be used for this scraper. Settings are automatically checked for updates every time scrapers are imported.
        The list returned by each scraper is aggregated into a big settings.xml string, and then if it differs from the current settings xml in the Scrapers category,
        the existing settings.xml fragment is removed and replaced by the new string.
        """
        name = cls.get_name()
        return [
            f'         <setting id="{name}-enable" type="bool" label="{name} {i18n("enabled")}" default="true" visible="true"/>',
            f'         <setting id="{name}-base_url" type="text" label="    {i18n("base_url")}" default="{cls.base_url}" visible="eq(-1,true)"/>',
            f'         <setting id="{name}-sub_check" type="bool" label="    {i18n("page_existence")}" default="true" visible="eq(-2,true)"/>',
        ]

    @classmethod
    def has_proxy(cls):
        return False

    def _default_get_url(self, video):
        """
        Retrieves the URL for a given video based on its type and metadata.

        Args:
            video (ScraperVideo): An object containing metadata about the video.
                - video_type: One of VIDEO_TYPES (e.g., EPISODE, TVSHOW, SEASON).
                - title: The title of the TV show or movie.
                - year: The year of the TV show or movie.
                - season: The season number (only for TV shows).
                - episode: The episode number (only for TV shows).

        Returns:
            str: The URL related to the video, or None if no URL is found.
        """
        url = None
        temp_video_type = video.video_type
        if video.video_type == VIDEO_TYPES.EPISODE:
            if VIDEO_TYPES.TVSHOW in self.provides():
                temp_video_type = VIDEO_TYPES.TVSHOW
            elif VIDEO_TYPES.SEASON in self.provides():
                temp_video_type = VIDEO_TYPES.SEASON

        season = video.season if temp_video_type == VIDEO_TYPES.SEASON else ''
        if temp_video_type != VIDEO_TYPES.EPISODE:
            result = self.db_connection().get_related_url(temp_video_type, video.title, video.year, self.get_name(), season)
            if result:
                url = result[0][0]
                logger.log('Got local related url: |%s|%s|%s|%s|%s|%s|' % (temp_video_type, video.title, video.year, season, self.get_name(), url), log_utils.LOGDEBUG)
            else:
                results = self.search(temp_video_type, video.title, video.year, season)
                if results:
                    url = results[0]['url']
                    self.db_connection().set_related_url(temp_video_type, video.title, video.year, self.get_name(), url, season)

        if isinstance(url, str): url = url
        if video.video_type == VIDEO_TYPES.EPISODE:
            if url == FORCE_NO_MATCH:
                url = None
            elif url or temp_video_type == VIDEO_TYPES.EPISODE:
                result = self.db_connection().get_related_url(VIDEO_TYPES.EPISODE, video.title, video.year, self.get_name(), video.season, video.episode)
                if result:
                    url = result[0][0]
                    if isinstance(url, str): url = url
                    logger.log('Got local related url: |%s|%s|%s|' % (video, self.get_name(), url), log_utils.LOGDEBUG)
                else:
                    url = self._get_episode_url(url, video)
                    if url:
                        self.db_connection().set_related_url(VIDEO_TYPES.EPISODE, video.title, video.year, self.get_name(), url, video.season, video.episode)

        return url

    def _http_get(self, url, params=None, data=None, multipart_data=None, headers=None, cookies=None, allow_redirect=True, method=None, require_debrid=False, read_error=False, cache_limit=8):
        if isinstance(url, bytes):
            url = url.decode('utf-8')

        html = self._cached_http_get(url, self.base_url, self.timeout, params=params, data=data, multipart_data=multipart_data,
                                     headers=headers, cookies=cookies, allow_redirect=allow_redirect, method=method, require_debrid=require_debrid,
                                     read_error=read_error, cache_limit=cache_limit)
        sucuri_cookie = scraper_utils.get_sucuri_cookie(html)
        if sucuri_cookie:
            logger.log('Setting sucuri cookie: %s' % (sucuri_cookie), log_utils.LOGDEBUG)
            if cookies is not None:
                cookies = cookies.update(sucuri_cookie)
            else:
                cookies = sucuri_cookie
            html = self._cached_http_get(url, self.base_url, self.timeout, params=params, data=data, multipart_data=multipart_data,
                                         headers=headers, cookies=cookies, allow_redirect=allow_redirect, method=method, require_debrid=require_debrid,
                                         read_error=read_error, cache_limit=0)
        return html
    
    def _cached_http_get(self, url, base_url, timeout, params=None, data=None, multipart_data=None, headers=None, cookies=None, allow_redirect=True,
                        method=None, require_debrid=False, read_error=False, cache_limit=8):
        if require_debrid:
            if Scraper.debrid_resolvers is None:
                Scraper.debrid_resolvers = [resolver for resolver in resolveurl.resolve(url) if resolver.isUniversal()]
            if not Scraper.debrid_resolvers:
                logger.log('%s requires debrid: %s' % (self.__module__, Scraper.debrid_resolvers), log_utils.LOGDEBUG)
                return ''

        if cookies is None: cookies = {}
        if timeout == 0: timeout = None
        if headers is None: headers = {}
        if url.startswith('//'): url = 'http:' + url
        referer = headers['Referer'] if 'Referer' in headers else base_url
        if params:
            if url == base_url and not url.endswith('/'):
                url += '/'
            
            parts = urllib.parse.urlparse(url)
            if parts.query:
                params.update(scraper_utils.parse_query(url))
                url = urllib.parse.urlunparse((parts.scheme, parts.netloc, parts.path, parts.params, '', parts.fragment))
                
            url += '?' + urllib.parse.urlencode(params)

        logger.log('Getting Url: %s cookie=|%s| data=|%s| extra headers=|%s|' % (url, cookies, data, headers), log_utils.LOGDEBUG)
        if data is not None:
            if isinstance(data, str):
                data = data
            else:
                data = urllib.parse.urlencode(data, True)

        if multipart_data is not None:
            headers['Content-Type'] = 'multipart/form-data; boundary=X-X-X'
            data = multipart_data

        _created, _res_header, html = self.db_connection().get_cached_url(url, data, cache_limit)
        if html:
            logger.log('Returning cached result for: %s' % (url), log_utils.LOGDEBUG)
            return html

        try:
            self.cj = self._set_cookies(base_url, cookies)
            if isinstance(url, str): url = url
            request = urllib.request.Request(url, data=data)
            headers = headers.copy()
            request.add_header('User-Agent', scraper_utils.get_ua())
            request.add_header('Accept', '*/*')
            request.add_header('Accept-Encoding', 'gzip')
            parsed_url = urllib.parse.urlparse(request.full_url)
            request.add_unredirected_header('Host', parsed_url.netloc)
            if referer: request.add_unredirected_header('Referer', referer)
            if 'Referer' in headers: del headers['Referer']
            if 'Host' in headers: del headers['Host']
            for key, value in headers.items(): request.add_header(key, value)
            self.cj.add_cookie_header(request)
            if not allow_redirect:
                opener = urllib.request.build_opener(NoRedirection)
                urllib.request.install_opener(opener)
            else:
                opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
                urllib.request.install_opener(opener)
                opener2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
                urllib.request.install_opener(opener2)

            if method is not None: request.get_method = lambda: method.upper()
            response = urllib.request.urlopen(request, timeout=timeout)
            self.cj.extract_cookies(response, request)

            if kodi.get_setting('cookie_debug') == 'true':
                logger.log('Response Cookies: %s - %s' % (url, scraper_utils.cookies_as_str(self.cj)), log_utils.LOGDEBUG)
            self.cj._cookies = scraper_utils.fix_bad_cookies(self.cj._cookies)
            self.cj.save(ignore_discard=True)

            if not allow_redirect and (response.getcode() in [301, 302, 303, 307] or response.headers.get('Refresh')):
                if response.headers.get('Refresh') is not None:
                    refresh = response.headers.get('Refresh')
                    return refresh.split(';')[-1].split('url=')[-1]
                else:
                    redir_url = response.headers.get('Location')
                    if redir_url.startswith('='):
                        redir_url = redir_url[1:]
                    return redir_url
            
            content_length = response.headers.get('Content-Length', 0)
            if int(content_length) > MAX_RESPONSE:
                logger.log('Response exceeded allowed size. %s => %s / %s' % (url, content_length, MAX_RESPONSE), log_utils.LOGWARNING)
            
            if method == 'HEAD':
                return ''
            else:
                if response.headers.get('Content-Encoding') == 'gzip':
                    html = ungz(response.read(MAX_RESPONSE))
                else:
                    html = response.read(MAX_RESPONSE)
        except urllib.error.HTTPError as e:
            if e.info().get('Content-Encoding') == 'gzip':
                html = ungz(e.read(MAX_RESPONSE))
            else:
                html = e.read(MAX_RESPONSE)
                
            if CF_CAPCHA_ENABLED and e.code == 403 and 'cf-captcha-bookmark' in html:
                html = cf_captcha.solve(url, self.cj, scraper_utils.get_ua(), self.get_name())
                if not html:
                    return ''
            elif e.code == 503 and 'cf-browser-verification' in html:
                html = cloudflare.solve(url, self.cj, scraper_utils.get_ua(), extra_headers=headers)
                if not html:
                    return ''
            else:
                logger.log('Error (%s) during scraper http get: %s' % (str(e), url), log_utils.LOGWARNING)
                if not read_error:
                    return ''
        except Exception as e:
            logger.log('Error (%s) during scraper http get: %s' % (str(e), url), log_utils.LOGWARNING)
            return ''

        self.db_connection().cache_url(url, html, data)
        return html

    def _set_cookies(self, base_url, cookies):
        cookie_file = os.path.join(COOKIEPATH, f'{self.get_name()}_cookies.lwp')
        cj = http.cookiejar.LWPCookieJar(cookie_file)
        try: cj.load(ignore_discard=True)
        except: pass
        if kodi.get_setting('cookie_debug') == 'true':
            logger.log(f'Before Cookies: {self} - {scraper_utils.cookies_as_str(cj)}', log_utils.LOGDEBUG)
        domain = urllib.parse.urlsplit(base_url).hostname
        for key in cookies:
            c = http.cookiejar.Cookie(0, key, str(cookies[key]), port=None, port_specified=False, domain=domain, domain_specified=True,
                                 domain_initial_dot=False, path='/', path_specified=True, secure=False, expires=None, discard=False, comment=None,
                                 comment_url=None, rest={})
            cj.set_cookie(c)
        cj.save(ignore_discard=True)
        if kodi.get_setting('cookie_debug') == 'true':
            logger.log(f'After Cookies: {self} - {scraper_utils.cookies_as_str(cj)}', log_utils.LOGDEBUG)
        return cj

    def _do_recaptcha(self, key, tries=None, max_tries=None):
        challenge_url = CAPTCHA_BASE_URL + '/challenge?k=%s' % (key)
        html = self._cached_http_get(challenge_url, CAPTCHA_BASE_URL, timeout=DEFAULT_TIMEOUT, cache_limit=0)
        match = re.search("challenge\s+\:\s+'([^']+)", html)
        captchaimg = 'http://www.google.com/recaptcha/api.js/image?c=%s' % (match.group(1))
        img = xbmcgui.ControlImage(450, 0, 400, 130, captchaimg)
        wdlg = xbmcgui.WindowDialog()
        wdlg.addControl(img)
        wdlg.show()
        header = 'Type the words in the image'
        if tries and max_tries:
            header += ' (Try: %s/%s)' % (tries, max_tries)
        solution = kodi.get_keyboard(header)
        if not solution:
            raise Exception('You must enter text in the image to access video')
        wdlg.close()
        return {'recaptcha_challenge_field': match.group(1), 'recaptcha_response_field': solution}

    def do_flaresolver(self, url):
        """
        Solves reCAPTCHA using FLARESOLVER.

        Args:
            url (str): The URL of the page containing the reCAPTCHA.
            site_key (str): The site key of the reCAPTCHA.

        Returns:
            str: The reCAPTCHA token if successful, otherwise None.
        """
        try:
            # Prepare the payload for FLARESOLVER
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000,
                "session": True,
                "cookies": True,
                "userAgent": scraper_utils.get_ua()
            }

            # Send the request to FLARESOLVER
            response = requests.post(f'{FLARESOLVERR_URL}/v1', json=payload)
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'ok':
                    return data.get('solution', {})
            logger.log(f'Failed FLARESOLVER: {response.text}', log_utils.LOGERROR)
        except Exception as e:
            logger.log(f'Error FLARESOLVER: {str(e)}', log_utils.LOGERROR)
        return None

    def _default_get_episode_url(self, html, video, episode_pattern, title_pattern='', airdate_pattern=''):
        logger.log('Default Episode Url: |%s|%s|' % (self.get_name(), video), log_utils.LOGDEBUG)
        if not html: return
        
        try: html = html[0].content
        except AttributeError: pass
        force_title = scraper_utils.force_title(video)
        if not force_title:
            if episode_pattern:
                match = re.search(episode_pattern, html, re.DOTALL | re.I)
                if match:
                    return scraper_utils.pathify_url(match.group(1))

            if kodi.get_setting('airdate-fallback') == 'true' and airdate_pattern and video.ep_airdate:
                airdate_pattern = airdate_pattern.replace('{year}', str(video.ep_airdate.year))
                airdate_pattern = airdate_pattern.replace('{month}', str(video.ep_airdate.month))
                airdate_pattern = airdate_pattern.replace('{p_month}', '%02d' % (video.ep_airdate.month))
                airdate_pattern = airdate_pattern.replace('{month_name}', MONTHS[video.ep_airdate.month - 1])
                airdate_pattern = airdate_pattern.replace('{short_month}', SHORT_MONS[video.ep_airdate.month - 1])
                airdate_pattern = airdate_pattern.replace('{day}', str(video.ep_airdate.day))
                airdate_pattern = airdate_pattern.replace('{p_day}', '%02d' % (video.ep_airdate.day))
                logger.log('Air Date Pattern: %s' % (airdate_pattern), log_utils.LOGDEBUG)

                match = re.search(airdate_pattern, html, re.DOTALL | re.I)
                if match:
                    return scraper_utils.pathify_url(match.group(1))
        else:
            logger.log('Skipping S&E matching as title search is forced on: %s' % (video.trakt_id), log_utils.LOGDEBUG)

        if (force_title or kodi.get_setting('title-fallback') == 'true') and video.ep_title and title_pattern:
            norm_title = scraper_utils.normalize_title(video.ep_title)
            for match in re.finditer(title_pattern, html, re.DOTALL | re.I):
                episode = match.groupdict()
                if norm_title == scraper_utils.normalize_title(episode['title']):
                    return scraper_utils.pathify_url(episode['url'])

    def _blog_proc_results(self, html, post_pattern, date_format, video_type, title, year):
        results = []
        search_date = ''
        search_sxe = ''
        if video_type == VIDEO_TYPES.EPISODE:
            match = re.search('(.*?)\s*(S\d+E\d+)\s*', title)
            if match:
                show_title, search_sxe = match.groups()
            else:
                match = re.search('(.*?)\s*(\d{4})[._ -]?(\d{2})[._ -]?(\d{2})\s*', title)
                if match:
                    show_title, search_year, search_month, search_day = match.groups()
                    search_date = '%s-%s-%s' % (search_year, search_month, search_day)
                    search_date = scraper_utils.to_datetime(search_date, "%Y-%m-%d").date()
                else:
                    show_title = title
        else:
            show_title = title

        today = datetime.date.today()
        for match in re.finditer(post_pattern, html, re.DOTALL):
            post_data = match.groupdict()
            post_title = post_data['post_title']
            post_title = re.sub('<[^>]*>', '', post_title)
            if 'quality' in post_data:
                post_title += '- [%s]' % (post_data['quality'])

            try: filter_days = int(kodi.get_setting('%s-filter' % (self.get_name())))
            except ValueError: filter_days = 0
            if filter_days and date_format and 'date' in post_data:
                post_data['date'] = post_data['date'].strip()
                filter_days = datetime.timedelta(days=filter_days)
                post_date = scraper_utils.to_datetime(post_data['date'], date_format).date()
                if not post_date:
                    logger.log('Failed date Check in %s: |%s|%s|%s|' % (self.get_name(), post_data['date'], date_format), log_utils.LOGWARNING)
                    post_date = today
                        
                if today - post_date > filter_days:
                    continue

            match_year = ''
            match_date = ''
            match_sxe = ''
            match_title = full_title = post_title
            if video_type == VIDEO_TYPES.MOVIE:
                meta = scraper_utils.parse_movie_link(post_title)
                match_year = meta['year']
            else:
                meta = scraper_utils.parse_episode_link(post_title)
                match_sxe = 'S%02dE%02d' % (int(meta['season']), int(meta['episode']))
                match_date = meta['airdate']

            match_title = meta['title']
            full_title = '%s (%sp) [%s]' % (meta['title'], meta['height'], meta['extra'])
            norm_title = scraper_utils.normalize_title(show_title)
            match_norm_title = scraper_utils.normalize_title(match_title)
            title_match = norm_title and (match_norm_title in norm_title or norm_title in match_norm_title)
            year_match = not year or not match_year or year == match_year
            sxe_match = not search_sxe or (search_sxe == match_sxe)
            date_match = not search_date or (search_date == match_date)
            logger.log('Blog Results: |%s|%s|%s| - |%s|%s|%s| - |%s|%s|%s| - |%s|%s|%s| (%s)' %
                          (match_norm_title, norm_title, title_match, year, match_year, year_match,
                           search_date, match_date, date_match, search_sxe, match_sxe, sxe_match, self.get_name()),
                          log_utils.LOGDEBUG)
            if title_match and year_match and date_match and sxe_match:
                quality = scraper_utils.height_get_quality(meta['height'])
                result = {'url': scraper_utils.pathify_url(post_data['url']), 'title': scraper_utils.cleanse_title(full_title), 'year': match_year, 'quality': quality}
                results.append(result)
        return results
    
    def _blog_get_url(self, video, delim='.'):
        url = None
        result = self.db_connection().get_related_url(video.video_type, video.title, video.year, self.get_name(), video.season, video.episode)
        if result:
            url = result[0][0]
            logger.log('Got local related url: |%s|%s|%s|%s|%s|' % (video.video_type, video.title, video.year, self.get_name(), url), log_utils.LOGDEBUG)
        else:
            try: select = int(kodi.get_setting('%s-select' % (self.get_name())))
            except: select = 0
            if video.video_type == VIDEO_TYPES.EPISODE:
                temp_title = re.sub('[^A-Za-z0-9 ]', '', video.title)
                if not scraper_utils.force_title(video):
                    search_title = '%s S%02dE%02d' % (temp_title, int(video.season), int(video.episode))
                    if isinstance(video.ep_airdate, datetime.date):
                        fallback_search = '%s %s' % (temp_title, video.ep_airdate.strftime('%Y{0}%m{0}%d'.format(delim)))
                    else:
                        fallback_search = ''
                else:
                    if not video.ep_title: return None
                    search_title = '%s %s' % (temp_title, video.ep_title)
                    fallback_search = ''
            else:
                search_title = video.title
                fallback_search = ''

            results = self.search(video.video_type, search_title, video.year)
            if not results and fallback_search:
                results = self.search(video.video_type, fallback_search, video.year)
                
            if results:
                # TODO: First result isn't always the most recent...
                best_result = results[0]
                if select != 0:
                    best_qorder = 0
                    for result in results:
                        if 'quality' in result:
                            quality = result['quality']
                        else:
                            match = re.search('\((\d+p)\)', result['title'])
                            if match:
                                quality = scraper_utils.height_get_quality(match.group(1))
                            else:
                                match = re.search('\[(.*)\]$', result['title'])
                                q_str = match.group(1) if match else ''
                                quality = scraper_utils.blog_get_quality(video, q_str, '')
                                
                        logger.log('result: |%s|%s|%s|' % (result, quality, Q_ORDER[quality]), log_utils.LOGDEBUG)
                        if Q_ORDER[quality] > best_qorder:
                            logger.log('Setting best as: |%s|%s|%s|' % (result, quality, Q_ORDER[quality]), log_utils.LOGDEBUG)
                            best_result = result
                            best_qorder = Q_ORDER[quality]

                url = best_result['url']
                self.db_connection().set_related_url(video.video_type, video.title, video.year, self.get_name(), url, video.season, video.episode)
        return url


    def _get_direct_hostname(self, link):
        """
        Determines the direct hostname for a given link.

        Args:
            link (str): The URL link to check.

        Returns:
            str: The direct hostname if recognized, otherwise the scraper's name.
        """
        host = urllib.parse.urlparse(link).hostname
        direct_hosts = ['google', 'orion', 'blogspot', 'okru', 'filemoon', 'mixdrop', 'vidcloud', 'embtaku', 'streamtape', 'dood', 'vidlox', 'mp4upload']

        if host and any(h in host for h in direct_hosts):
            return 'gvideo'
        else:
            return self.get_name()
    
    def _parse_google(self, link):
        sources = []
        html = self._http_get(link, cache_limit=.25)
        match = re.search('pid=([^&]+)', link)
        if match:
            vid_id = match.group(1)
            sources = self.__parse_gplus(vid_id, html, link)
        else:
            if 'drive.google' in link or 'docs.google' in link:
                sources = self._parse_gdocs(link)
            if 'picasaweb' in link:
                i = link.rfind('#')
                if i > -1:
                    link_id = link[i + 1:]
                else:
                    link_id = ''
                match = re.search('feedPreload:\s*(.*}]}})},', html, re.DOTALL)
                if match:
                    js = scraper_utils.parse_json(match.group(1), link)
                    for item in js['feed']['entry']:
                        if not link_id or item['gphoto$id'] == link_id:
                            for media in item['media']['content']:
                                if media['type'].startswith('video'):
                                    sources.append(media['url'].replace('%3D', '='))
                else:
                    match = re.search('preload\'?:\s*(.*}})},', html, re.DOTALL)
                    if match:
                        js = scraper_utils.parse_json(match.group(1), link)
                        for media in js['feed']['media']['content']:
                            if media['type'].startswith('video'):
                                sources.append(media['url'].replace('%3D', '='))

        sources = list(set(sources))
        return sources

    def __parse_gplus(self, vid_id, html, link=''):
        sources = []
        match = re.search('return\s+(\[\[.*?)\s*}}', html, re.DOTALL)
        if match:
            try:
                js = scraper_utils.parse_json(match.group(1), link)
                for top_item in js:
                    if isinstance(top_item, list):
                        for item in top_item:
                            if isinstance(item, list):
                                for item2 in item:
                                    if isinstance(item2, list):
                                        for item3 in item2:
                                            if item3 == vid_id:
                                                sources = self.__extract_video(item2)
            except Exception as e:
                log_utils.log('Google Plus Parse failure: %s - %s' % (link, e), log_utils.LOGWARNING)
        return sources

    def __extract_video(self, item):
        """
        Extracts video URLs from a nested list structure.

        Args:
            item (list): A nested list structure containing video URLs.

        Returns:
            list: A list of extracted video URLs.
        """
        sources = []
        for e in item:
            if isinstance(e, dict):
                for key in e:
                    for item2 in e[key]:
                        if isinstance(item2, list):
                            for item3 in item2:
                                if isinstance(item3, list):
                                    for item4 in item3:
                                        if isinstance(item4, str):
                                            s = urllib.parse.unquote(item4).replace('\\0026', '&').replace('\\003D', '=')
                                            sources.extend(re.findall('url=([^&]+)', s))
        return sources
        
    def _parse_gdocs(self, link):
        urls = []
        html = self._http_get(link, cache_limit=.5)
        for match in re.finditer('\[\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\]', html):
            key, value = match.groups()
            if key == 'fmt_stream_map':
                items = value.split(',')
                for item in items:
                    _source_fmt, source_url = item.split('|')
                    source_url = source_url.replace('\\u003d', '=').replace('\\u0026', '&')
                    source_url = urllib.parse.unquote(source_url)
                    source_url += '|Cookie=%s' % (self._get_stream_cookies())
                    urls.append(source_url)
                    
        return urls

    def _get_cookies(self):
        cj = self._set_cookies(self.base_url, {})
        cookies = dict((cookie.name, cookie.value) for cookie in cj)
        return cookies
        
    def _get_stream_cookies(self):
        cookies = ['%s=%s' % (key, value) for key, value in self._get_cookies().items()]
        return urllib.parse.quote('; '.join(cookies))

    def db_connection(self):
        if self.__db_connection is None:
            self.__db_connection = DB_Connection()
        return self.__db_connection
        
    def _parse_sources_list(self, html):
        sources = {}
        match = re.search('sources\s*:\s*\[(.*?)\]', html, re.DOTALL)
        if not match:
            match = re.search('sources\s*:\s*\{(.*?)\}', html, re.DOTALL)
            
        if match:
            for match in re.finditer('''['"]?file['"]?\s*:\s*['"]([^'"]+)['"][^}]*['"]?label['"]?\s*:\s*['"]([^'"]*)''', match.group(1), re.DOTALL):
                stream_url, label = match.groups()
                stream_url = stream_url.replace('\/', '/')
                if self._get_direct_hostname(stream_url) == 'gvideo':
                    sources[stream_url] = {'quality': scraper_utils.gv_get_quality(stream_url), 'direct': True}
                elif re.search('\d+p?', label, re.I):
                    sources[stream_url] = {'quality': scraper_utils.height_get_quality(label), 'direct': True}
                else:
                    sources[stream_url] = {'quality': label, 'direct': True}
        return sources

    def _get_files(self, url, headers=None, cache_limit=.5):
        """
        Retrieves files from a directory URL.

        Args:
            url (str): The URL of the directory to fetch files from.
            headers (dict): Optional headers for the HTTP request.
            cache_limit (float): Cache limit for the request.

        Returns:
            list: A list of file rows with URLs and metadata.
        """
        sources = []
        for row in self._parse_directory(self._http_get(url, headers=headers, cache_limit=cache_limit)):
            source_url = scraper_utils.urljoin(url, row['link'])
            if row['directory'] and not row['link'].startswith('..'):
                sources += self._get_files(source_url, headers={'Referer': url}, cache_limit=cache_limit)
            else:
                row['url'] = source_url
                sources.append(row)
        return sources
    
    def _parse_directory(self, html):
        """
        Parses the HTML of a directory listing to extract file information.

        Args:
            html (str): The HTML content of the directory listing.

        Returns:
            list: A list of dictionaries containing file information.
        """
        rows = []
        for match in re.finditer(self.row_pattern, html):
            row = match.groupdict()
            if row['title'].endswith('/'): row['title'] = row['title'][:-1]
            row['directory'] = True if row['link'].endswith('/') else False
            if row['size'] == '-': row['size'] = None
            rows.append(row)
        return rows
