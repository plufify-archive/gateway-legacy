import asyncio
import zlib
import json
import ulid
from typing import Sequence, Any, Set, Union, Dict, List
from websockets import server, exceptions
from .db import users, members, guilds, channels, presences


def yield_chunks(input_list: Sequence[Any], chunk_size: int):
    for idx in range(0, len(input_list), chunk_size):
        yield input_list[idx : idx + chunk_size]


def byte(data: Union[str, bytes]) -> bytes:
    return data if isinstance(data, bytes) else data.encode()


secret = 'adb8ddecad0ec633da6651a1b441026fdc646892'
sessions = []


class GatewayConnection:
    def __init__(self, ws: server.WebSocketServerProtocol, encoding: str):
        self.ws = ws
        self.closed: bool = False
        self.encoding = encoding

        # intitiate zlib things
        self.deflator = zlib.compressobj()
        self.user_info = None
        self.session_id = None
        self.presences: bool = False

    async def check_session_id(self):
        while True:
            if self.closed:
                break

            if self.session_id == secret:
                # we don't need to check.
                break

            if await users.find_one({'session_ids': [self.session_id]}) == None:
                await self.ws.close(4002, 'Invalid authorization')
                break
            else:
                find = await users.find_one({'session_ids': [self.session_id]})
                to_give = {
                    '_id': find['_id'],
                    'username': find['username'],
                    'separator': find['separator'],
                    'avatar_url': find['avatar_url'],
                    'banner_url': find['banner_url'],
                    'flags': find['flags'],
                    'verified': find['verified'],
                    'system': find['system'],
                    'session_ids': find['session_ids'],
                }
                self.user_info = json.dumps(to_give)
                break

    async def __send(self, data: bytes, chunk_size: int):
        await self.ws.send(yield_chunks(data, chunk_size))

    async def _send(self, data):
        d1 = self.deflator.compress(data)
        d2 = self.deflator.flush(zlib.Z_FULL_FLUSH)
        d = d1 + d2

        await self.__send(d, 1024)

    async def send(self, payload: Any):
        if isinstance(payload, dict):
            if self.encoding == 'json':
                await self.ws.send(json.dumps(payload))
            else:
                await self._send(byte(json.dumps(payload)))
        else:
            await self._send(byte(payload))

    async def do_hello(self):
        await self.send(
            {
                't': 'HELLO',
                's': self._user_session_id,
                'd': None,
                'i': 'Sent once we have verified your session_id, '
                'the data given will be null. '
                'please wait for the READY event before continuing-'
                '-with any requests.',
            }
        )

    async def do_ready(self):
        await self.send(
            {
                't': 'READY',
                's': self.session_id,
                'd': self.user_info,
                'i': None,
            }
        )

        if self.session_id == secret:
            return

        membered = await members.find({'user': {'_id': self.user_info['_id']}})

        guilds_to_give: list[dict] = []

        for member in membered:
            obj: dict = await guilds.find_one({'_id': member['guild_id']})
            obj2: dict = await channels.find_one({'guild_id': member['guild_id']})
            obj['channels'] = obj2

            guilds_to_give.append(obj)

        for guild in guilds_to_give:
            await self.send(
                {'t': 'GUILD_INIT', 's': self._user_session_id, 'd': guild, 'i': ''}
            )

    async def poll_recv(self, data: dict):
        if data.get('t', '') == 'HEARTBEAT':
            await self.send(
                {
                    't': 'ACK',
                    '_s': self.session_id,
                    's': data.get('s', ''),
                    'd': None,
                }
            )
        elif data.get('t', '') == 'DISPATCH':
            if self.session_id != secret:
                await self.ws.close(4004, 'Invalid Dispatch Sent')
                self.closed = True
                return
            else:
                d = data.get('d')
                await dispatch_event(d['name'], d['data'])

        elif data.get('t', '') == 'DISPATCH_TO':
            if self.session_id != secret:
                await self.ws.close(4004, 'Invalid Dispatch Sent')
                self.closed = True
                return

            _d = data.get('d')
            d = {'t': _d['event_name'].upper(), 'd': _d['data']}
            s = await users.find_one({'_id': _d['user']})
            for connection in connections:
                for session_id in s['session_ids']:
                    if session_id == connection.session_id:
                        await connection.send(d)

        elif data.get('t', '') == 'DISPATCH_TO_GUILD':
            if self.session_id != secret:
                await self.ws.close(4004, 'Invalid Dispatch Sent')
                self.closed = True
                return

            ms = members.find({'guild_id': data['guild_id']})
            _d = data.get('d')
            d = {'t': _d['event_name'].upper(), 'd': _d['data']}

            for connection in connections:
                for member in ms:
                    for session_id in member['session_ids']:
                        if session_id == connection.session_id:
                            await connection.send(d)

        elif data.get('t', '') == 'NOTIFICATION':
            if self.session_id != secret:
                # could be a mistake?
                return

            user = await users.find_one({'_id': data['_id']})

            data = {
                't': 'NOTIFICATION',
                'type': data.get('type'),
                'excerpt': data.get('excerpt'),
            }

            for connection in connections:
                for session_id in user['session_ids']:
                    if session_id == connection.session_id:
                        await connection.send(data)

        elif data.get('t', '') == 'PRESENCE':
            if data.get('type', '') not in (1, 2, 3, 4):
                return

            try:
                if data.get('embed'):
                    em = data.get('embed')
                    embed = {
                        'name': str(em['name']),
                        'description': str(em['description']),
                        'banner_url': str(em.get('banner_url')),
                        'text': {
                            'top': str(em.get('top_text')),
                            'bottom': str(em.get('bottom_text')),
                        },
                    }
                else:
                    embed = None

            except:
                return

            try:
                d = {
                    '_id': self.user_info['_id'],
                    'd': {
                        'type': data['type'],
                        'description': data['description'],
                        'emoji': data.get('emoji'),
                        'embed': embed,
                    },
                }
            except KeyError:
                return
            dis = d.copy()
            await presences.insert_one(d)

            dis['t'] = 'PRESENCE_UPDATE'

            ms = members.find({'_id': self.user_info['_id']})

            for member in ms:
                _mems = await guilds.find_one({'guild_id': member['guild_id']})
                for connection in connections:
                    for mem in _mems:
                        for session_id in mem['session_ids']:
                            if session_id == connection.session_id:
                                await connection.send(dis)

    async def do_recv(self):
        while True:
            if self.closed:
                connections.remove(self)
                del self
                break

            try:
                r = await self.ws.recv()
                await self.poll_recv(json.loads(r))
            except exceptions.ConnectionClosedOK:
                self.closed = True
                break

    async def run(self, data: dict):
        if self.encoding not in ('json', 'zlib'):
            await self.ws.close(4004, 'Invalid encoding')
            self.closed = True
            return

        connections.add(self)
        self.session_id = data.get('session_id', '')
        await self.check_session_id()

        self._user_session_id = ulid.new().str

        sessions.append(self.session_id)

        self.presences = data.get('presences', False)

        if self.presences not in (True, False):
            await self.ws.close(4001, 'Presence has to be a bool.')

        await self.do_hello()
        await asyncio.sleep(9)
        await self.do_ready()
        try:
            await self.do_recv()
        except exceptions.ConnectionClosedError:
            try:
                sessions.remove(self.session_id)
            except ValueError:
                del self._user_session_id
                del self.session_id
            return


connections: Set[GatewayConnection] = set()
# 'adb8ddecad0ec633da6651a1b441026fdc646892'
async def dispatch_event(event_name: str, data: dict):
    d = {'t': event_name.upper(), 'd': data}
    for connection in connections.copy():
        await connection.send(d)
