"""

Litecord
Copyright (C) 2018-2019  Luna Mendes

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, version 3 of the License.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""

"""
litecord.embed.sanitizer
    sanitize embeds by giving common values
    such as type: rich
"""
from typing import Dict, Any, Optional, Union, List, Tuple

from logbook import Logger
from quart import current_app as app

from litecord.embed.schemas import EmbedURL

log = Logger(__name__)
Embed = Dict[str, Any]


def sanitize_embed(embed: Embed) -> Embed:
    """Sanitize an embed object.

    This is non-complex sanitization as it doesn't
    need the app object.
    """
    return {**embed, **{"type": "rich"}}


def path_exists(embed: Embed, components_in: Union[List[str], str]):
    """Tell if a given path exists in an embed (or any dictionary).

    The components string is formatted like this:
        key1.key2.key3.key4. <...> .keyN

    with each key going deeper and deeper into the embed.
    """

    # get the list of components given
    if isinstance(components_in, str):
        components = components_in.split(".")
    else:
        components = list(components_in)

    # if there are no components, we reached the end of recursion
    # and can return true
    if not components:
        return True

    # extract current component
    current = components[0]

    # if it exists, then we go down a level inside the dict
    # (via recursion)
    try:
        return path_exists(embed[current], components[1:])
    except (KeyError, TypeError, ValueError):
        # if the current component doesn't exist or we can't do a
        # key fetch, return False
        return False


def _mk_cfg_sess(config, session) -> tuple:
    """Return a tuple of (config, session)."""
    if config is None:
        config = app.config

    if session is None:
        session = app.session

    return config, session


def _md_base(config) -> Optional[tuple]:
    """Return the protocol and base url for the mediaproxy."""
    md_base_url = config["MEDIA_PROXY"]
    if md_base_url is None:
        return None

    proto = "https" if config["IS_SSL"] else "http"

    return proto, md_base_url


def make_md_req_url(config, scope: str, url):
    """Make a mediaproxy request URL given the config, scope, and the url
    to be proxied.

    When MEDIA_PROXY is None, however, returns the original URL.
    """
    base = _md_base(config)
    if base is None:
        return url.url if isinstance(url, EmbedURL) else url

    proto, base_url = base
    return f"{proto}://{base_url}/{scope}/{url.to_md_path}"


def proxify(url, *, config=None) -> str:
    """Return a mediaproxy url for the given EmbedURL. Returns an
    /img/ scope."""
    config, _sess = _mk_cfg_sess(config, False)

    if isinstance(url, str):
        url = EmbedURL(url)

    return make_md_req_url(config, "img", url)


async def _md_client_req(
    config, session, scope: str, url, *, ret_resp=False
) -> Optional[Union[Tuple, Dict]]:
    """Makes a request to the mediaproxy.

    This has common code between all the main mediaproxy request functions
    to decrease code repetition.

    Note that config and session exist because there are cases where the app
    isn't retrievable (as those functions usually run in background tasks,
    not in the app itself).

    Parameters
    ----------
    config: dict-like
        the app configuration, if None, this will get the global one from the
        app instance.
    session: aiohttp client session
        the aiohttp ClientSession instance to use, if None, this will get
        the global one from the app.

    scope: str
        the scope of your request. one of 'meta', 'img', or 'embed' are
        available for the mediaproxy's API.
    url: string or EmbedURL
        the url in question to give to the mediaproxy.

    ret_resp: bool, default false
        if this function returns the response and its bytes as a tuple, instead
        of the raw json object. used by 'img' scope to proxy images, as we want
        the raw bytes of the response, but by the time this function is
        returned, the response object is invalid and the socket is closed
    """
    config, session = _mk_cfg_sess(config, session)

    if not isinstance(url, EmbedURL):
        url = EmbedURL(url)

    request_url = make_md_req_url(config, scope, url)

    async with session.get(request_url) as resp:
        if resp.status == 200:
            if ret_resp:
                return resp, await resp.read()

            return await resp.json()

        body = await resp.text()
        log.warning("failed to call {!r}, {} {!r}", request_url, resp.status, body)
        return None


async def fetch_metadata(url, *, config=None, session=None) -> Optional[Dict]:
    """Fetch metadata for a url (image width, mime, etc)."""
    return await _md_client_req(config, session, "meta", url)


async def fetch_raw_img(url, *, config=None, session=None) -> Optional[tuple]:
    """Fetch raw data for a url (the bytes given off, used to proxy images).

    Returns a tuple containing the response object and the raw bytes given by
    the website.
    """
    tup = await _md_client_req(config, session, "img", url, ret_resp=True)

    if not tup:
        return None

    return tup


async def fetch_embed(url, *, config=None, session=None) -> Dict[str, Any]:
    """Fetch an embed for a given webpage (an automatically generated embed
    by the mediaproxy, look over the project on how it generates embeds).

    Returns a discord embed object.
    """
    return await _md_client_req(config, session, "embed", url)


async def fill_embed(embed: Optional[Embed]) -> Optional[Embed]:
    """Fill an embed with more information, such as proxy URLs.

    Uses path_exists() to check if a given element exists in an embed by
    checking if its parent fields also exist, which is why we do
    `path_exists(embed, 'footer.icon_url')`
    instead of
    `embed.get('icon_url', embed.get('footer', {}))`.

    Uses the proxify function so that clients don't directly contact websites
    in embeds and instead use the mediaproxy.
    """
    if embed is None:
        return None

    embed = sanitize_embed(embed)

    if path_exists(embed, "footer.icon_url"):
        embed["footer"]["proxy_icon_url"] = proxify(embed["footer"]["icon_url"])

    if path_exists(embed, "author.icon_url"):
        embed["author"]["proxy_icon_url"] = proxify(embed["author"]["icon_url"])

    if path_exists(embed, "image.url"):
        image_url = embed["image"]["url"]

        meta = await fetch_metadata(image_url)
        embed["image"]["proxy_url"] = proxify(image_url)

        if meta and meta["image"]:
            embed["image"]["width"] = meta["width"]
            embed["image"]["height"] = meta["height"]

    return embed
