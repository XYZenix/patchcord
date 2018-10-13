import random

from quart import Blueprint, jsonify, request, current_app as app
from asyncpg import UniqueViolationError

from ..auth import token_check
from ..snowflake import get_snowflake
from ..errors import Forbidden, BadRequest, Unauthorized
from ..schemas import validate, USER_SETTINGS, \
    CREATE_DM, CREATE_GROUP_DM, USER_UPDATE
from ..enums import ChannelType, RelationshipType

from .guilds import guild_check
from .auth import hash_data, check_password, check_username_usage

bp = Blueprint('user', __name__)


@bp.route('/@me', methods=['GET'])
async def get_me():
    """Get the current user's information."""
    user_id = await token_check()
    user = await app.storage.get_user(user_id, True)
    return jsonify(user)


@bp.route('/<int:target_id>', methods=['GET'])
async def get_other(target_id):
    """Get any user, given the user ID."""
    user_id = await token_check()

    bot = await app.db.fetchval("""
    SELECT bot FROM users
    WHERE users.id = $1
    """, user_id)

    if not bot:
        raise Forbidden('Only bots can use this endpoint')

    other = await app.storage.get_user(target_id)
    return jsonify(other)


async def _try_reroll(user_id, preferred_username: str = None):
    for _ in range(10):
        reroll = str(random.randint(1, 9999))

        if preferred_username:
            existing_uid = await app.db.fetchrow("""
            SELECT user_id
            FROM users
            WHERE preferred_username = $1 AND discriminator = $2
            """, preferred_username, reroll)

            if not existing_uid:
                return reroll

            continue

        try:
            await app.db.execute("""
            UPDATE users
            SET discriminator = $1
            WHERE users.id = $2
            """, reroll, user_id)

            return reroll
        except UniqueViolationError:
            continue

    return


async def _try_username_patch(user_id, new_username: str) -> str:
    await check_username_usage(new_username)
    discrim = None

    try:
        await app.db.execute("""
        UPDATE users
        SET username = $1
        WHERE users.id = $2
        """, new_username, user_id)

        return await app.db.fetchval("""
        SELECT discriminator
        FROM users
        WHERE users.id = $1
        """, user_id)
    except UniqueViolationError:
        discrim = await _try_reroll(user_id, new_username)

        if not discrim:
            raise BadRequest('Unable to change username', {
                'username': 'Too many people are with this username.'
            })

        await app.db.execute("""
        UPDATE users
        SET username = $1, discriminator = $2
        WHERE users.id = $3
        """, new_username, discrim, user_id)

    return discrim


async def _try_discrim_patch(user_id, new_discrim: str):
    try:
        await app.db.execute("""
        UPDATE users
        SET discriminator = $1
        WHERE id = $2
        """, new_discrim, user_id)
    except UniqueViolationError:
        raise BadRequest('Invalid discriminator', {
            'discriminator': 'Someone already used this discriminator.'
        })


def to_update(j: dict, user: dict, field: str):
    return field in j and j[field] and j[field] != user[field]


async def _check_pass(j, user):
    if not j['password']:
        raise BadRequest('password required', {
            'password': 'password required'
        })

    phash = user['password_hash']

    if not await check_password(phash, j['password']):
        raise BadRequest('password incorrect', {
            'password': 'password does not match.'
        })


@bp.route('/@me', methods=['PATCH'])
async def patch_me():
    """Patch the current user's information."""
    user_id = await token_check()

    j = validate(await request.get_json(), USER_UPDATE)
    user = await app.storage.get_user(user_id, True)

    user['password_hash'] = await app.db.fetchval("""
    SELECT password_hash
    FROM users
    WHERE id = $1
    """, user_id)

    if to_update(j, user, 'username'):
        # this will take care of regenning a new discriminator
        discrim = await _try_username_patch(user_id, j['username'])
        user['username'] = j['username']
        user['discriminator'] = discrim

    if to_update(j, user, 'discriminator'):
        # the API treats discriminators as integers,
        # but I work with strings on the database.
        new_discrim = str(j['discriminator'])

        await _try_discrim_patch(user_id, new_discrim)
        user['discriminator'] = new_discrim

    if to_update(j, user, 'email'):
        await _check_pass(j, user)

        # TODO: reverify the new email?
        await app.db.execute("""
        UPDATE users
        SET email = $1
        WHERE id = $2
        """, j['email'], user_id)
        user['email'] = j['email']

    if 'avatar' in j:
        # TODO: update icon
        pass

    if 'new_password' in j and j['new_password']:
        await _check_pass(j, user)

        new_hash = await hash_data(j['new_password'])

        await app.db.execute("""
        UPDATE users
        SET password_hash = $1
        WHERE id = $2
        """, new_hash, user_id)

    user.pop('password_hash')
    await app.dispatcher.dispatch_user(
        user_id, 'USER_UPDATE', user)

    public_user = await app.storage.get_user(user_id)

    guild_ids = await app.storage.get_user_guilds(user_id)
    friend_ids = await app.storage.get_friend_ids(user_id)

    await app.dispatcher.dispatch_many(
        'guild', guild_ids, 'USER_UPDATE', public_user
    )

    await app.dispatcher.dispatch_many(
        'friend', friend_ids, 'USER_UPDATE', public_user
    )

    return jsonify(user)


@bp.route('/@me/guilds', methods=['GET'])
async def get_me_guilds():
    """Get partial user guilds."""
    user_id = await token_check()
    guild_ids = await app.storage.get_user_guilds(user_id)

    partials = []

    for guild_id in guild_ids:
        partial = await app.db.fetchrow("""
        SELECT id::text, name, icon, owner_id
        FROM guilds
        WHERE guild_id = $1
        """, guild_id)

        # TODO: partial['permissions']
        partial['owner'] = partial['owner_id'] == user_id
        partial.pop('owner_id')

        partials.append(partial)

    return jsonify(partials)


@bp.route('/@me/guilds/<int:guild_id>', methods=['DELETE'])
async def leave_guild(guild_id: int):
    user_id = await token_check()
    await guild_check(user_id, guild_id)

    await app.db.execute("""
    DELETE FROM members
    WHERE user_id = $1 AND guild_id = $2
    """, user_id, guild_id)

    # first dispatch guild delete to the user,
    # then remove from the guild,
    # then tell the others that the member was removed
    await app.dispatcher.dispatch_user_guild(
        user_id, guild_id, 'GUILD_DELETE', {
            'id': str(guild_id),
            'unavailable': False,
        }
    )

    await app.dispatcher.unsub('guild', guild_id, user_id)

    await app.dispatcher.dispatch_guild('GUILD_MEMBER_REMOVE', {
        'guild_id': str(guild_id),
        'user': await app.storage.get_user(user_id)
    })

    return '', 204


# @bp.route('/@me/connections', methods=['GET'])
async def get_connections():
    pass


@bp.route('/@me/channels', methods=['GET'])
async def get_dms():
    user_id = await token_check()
    dms = await app.storage.get_dms(user_id)
    return jsonify(dms)


async def try_dm_state(user_id, dm_id):
    """Try insertin the user into the dm state
    for the given DM."""
    try:
        await app.db.execute("""
        INSERT INTO dm_channel_state (user_id, dm_id)
        VALUES ($1, $2)
        """, user_id, dm_id)
    except UniqueViolationError:
        # if already in state, ignore
        pass


async def create_dm(user_id, recipient_id):
    dm_id = get_snowflake()

    try:
        await app.db.execute("""
        INSERT INTO channels (id, channel_type)
        VALUES ($1, $2)
        """, dm_id, ChannelType.DM.value)

        await app.db.execute("""
        INSERT INTO dm_channels (id, party1_id, party2_id)
        VALUES ($1, $2, $3)
        """, dm_id, user_id, recipient_id)

        await try_dm_state(user_id, dm_id)

    except UniqueViolationError:
        # the dm already exists
        dm_id = await app.db.fetchval("""
        SELECT id
        FROM dm_channels
        WHERE (party1_id = $1 OR party2_id = $1) AND
              (party2_id = $2 OR party2_id = $2)
        """, user_id, recipient_id)

    dm = await app.storage.get_dm(dm_id, user_id)
    return jsonify(dm)


@bp.route('/@me/channels', methods=['POST'])
async def start_dm():
    """Create a DM with a user."""
    user_id = await token_check()
    j = validate(await request.get_json(), CREATE_DM)
    recipient_id = j['recipient_id']

    return await create_dm(user_id, recipient_id)


@bp.route('/<int:p_user_id>/channels', methods=['POST'])
async def create_group_dm(p_user_id: int):
    """Create a DM or a Group DM with user(s)."""
    user_id = await token_check()
    assert user_id == p_user_id

    j = validate(await request.get_json(), CREATE_GROUP_DM)
    recipients = j['recipients']

    if len(recipients) == 1:
        # its a group dm with 1 user... a dm!
        return await create_dm(user_id, int(recipients[0]))

    # TODO: group dms
    return 'group dms not implemented', 500


@bp.route('/@me/notes/<int:target_id>', methods=['PUT'])
async def put_note(target_id: int):
    """Put a note to a user."""
    user_id = await token_check()

    j = await request.get_json()
    note = str(j['note'])

    try:
        await app.db.execute("""
        INSERT INTO notes (user_id, target_id, note)
        VALUES ($1, $2, $3)
        """, user_id, target_id, note)
    except UniqueViolationError:
        await app.db.execute("""
        UPDATE notes
        SET note = $3
        WHERE user_id = $1 AND target_id = $2
        """, user_id, target_id, note)

    await app.dispatcher.dispatch_user(user_id, 'USER_NOTE_UPDATE', {
        'id': str(target_id),
        'note': note,
    })

    return '', 204


@bp.route('/@me/settings', methods=['GET'])
async def get_user_settings():
    """Get the current user's settings."""
    user_id = await token_check()
    settings = await app.storage.get_user_settings(user_id)
    return jsonify(settings)


@bp.route('/@me/settings', methods=['PATCH'])
async def patch_current_settings():
    user_id = await token_check()
    j = validate(await request.get_json(), USER_SETTINGS)

    for key in j:
        await app.db.execute(f"""
        UPDATE user_settings
        SET {key}=$1
        """, j[key])

    settings = await app.storage.get_user_settings(user_id)
    await app.dispatcher.dispatch_user(
        user_id, 'USER_SETTINGS_UPDATE', settings)
    return jsonify(settings)


@bp.route('/@me/consent', methods=['GET', 'POST'])
async def get_consent():
    """Always disable data collection.

    Also takes any data collection changes
    by the client and ignores them, as they
    will always be false.
    """
    return jsonify({
        'usage_statistics': {
            'consented': False,
        },
        'personalization': {
            'consented': False,
        }
    })


@bp.route('/@me/harvest', methods=['GET'])
async def get_harvest():
    """Dummy route"""
    return '', 204


@bp.route('/@me/activities/statistics/applications', methods=['GET'])
async def get_stats_applications():
    """Dummy route for info on gameplay time and such"""
    return jsonify([])


@bp.route('/@me/library', methods=['GET'])
async def get_library():
    """Probably related to Discord Store?"""
    return jsonify([])


@bp.route('/<int:peer_id>/profile', methods=['GET'])
async def get_profile(peer_id: int):
    """Get a user's profile."""
    user_id = await token_check()

    # TODO: check if they have any mutual guilds,
    # and return empty profile if they don't.
    peer = await app.storage.get_user(peer_id)

    if not peer:
        return '', 404

    # actual premium status is determined by that
    # column being NULL or not
    peer_premium = await app.db.fetchval("""
    SELECT premium_since
    FROM users
    WHERE id = $1
    """, peer_id)

    # this is a rad sql query
    mutual_guilds = await app.db.fetch("""
    SELECT guild_id FROM members WHERE user_id = $1
    INTERSECT
    SELECT guild_id FROM members WHERE user_id = $2
    """, user_id, peer_id)

    mutual_guilds = [r['guild_id'] for r in mutual_guilds]
    mutual_res = []

    # ascending sorting
    for guild_id in sorted(mutual_guilds):

        nick = await app.db.fetchval("""
        SELECT nickname
        FROM members
        WHERE guild_id = $1 AND user_id = $2
        """, guild_id, peer_id)

        mutual_res.append({
            'id': str(guild_id),
            'nick': nick,
        })

    return jsonify({
        'user': peer,
        'connected_accounts': [],
        'premium_since': peer_premium,
        'mutual_guilds': mutual_res,
    })


@bp.route('/<int:peer_id>/relationships', methods=['GET'])
async def get_mutual_friends(peer_id: int):
    user_id = await token_check()
    _friend = RelationshipType.FRIEND.value

    peer = await app.storage.get_user(peer_id)

    if not peer:
        return '', 204

    # NOTE: maybe this could be better with pure SQL calculations
    # but it would be beyond my current SQL knowledge, so...
    user_rels = await app.storage.get_relationships(user_id)
    peer_rels = await app.storage.get_relationships(peer_id)

    user_friends = {rel['user']['id']
                    for rel in user_rels if rel['type'] == _friend}
    peer_friends = {rel['user']['id']
                    for rel in peer_rels if rel['type'] == _friend}

    # get the intersection, then map them to Storage.get_user() calls
    mutual_ids = user_friends | peer_friends

    mutual_friends = []

    for friend_id in mutual_ids:
        mutual_friends.append(
            await app.storage.get_user(int(friend_id))
        )

    return jsonify(mutual_friends)
