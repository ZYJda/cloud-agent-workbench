import asyncio
import json

class EventStream:
    def __init__(self,store): self.store=store
    async def iterate(self, sid, cursor, request):
        while not await request.is_disconnected():
            rows = self.store.query('SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT 100',(sid,cursor))
            for row in rows:
                cursor = row['id']
                row['payload'] = json.loads(row['payload'])
                yield f'id: {cursor}\ndata: {json.dumps(row,ensure_ascii=False)}\n\n'
            if not rows:
                yield ': keepalive\n\n'
            await asyncio.sleep(.5)
