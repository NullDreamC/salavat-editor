"""One editable status message per incoming material; no invented percentages."""
import asyncio
from contextlib import suppress
from contextvars import ContextVar
import time


current_progress = ContextVar('bot_progress', default=None)


class Progress:
    def __init__(self, call, token, chat_id, received):
        self.call, self.token, self.chat_id = call, token, chat_id
        self.received = received
        self.message_id = None
        self.post_id = None
        self.stage = ''
        self.started = time.monotonic()
        self.lock = asyncio.Lock()

    async def update(self, stage=None, post_id=None):
        async with self.lock:
            if stage is not None:
                self.stage = stage
            if post_id is not None:
                self.post_id = post_id
            text = self.received
            if self.post_id is not None:
                text += f'\nПост №{self.post_id}'
            text += '\n' + self.stage
            seconds = int(time.monotonic() - self.started)
            if stage is None:
                text += f'\nПрошло {seconds // 60} мин {seconds % 60} с.'
            payload = {'chat_id': self.chat_id, 'text': text}
            try:
                if self.message_id is None:
                    result = await asyncio.wait_for(self.call(self.token, 'sendMessage', payload), timeout=10)
                    self.message_id = result['message_id']
                else:
                    await asyncio.wait_for(self.call(self.token, 'editMessageText', {**payload, 'message_id': self.message_id}), timeout=10)
            except Exception:
                # A failed status update must not discard an accepted material.
                pass

    async def heartbeat(self):
        while True:
            await asyncio.sleep(30)
            await self.update()


async def progress(stage, post_id=None):
    active = current_progress.get()
    if active is not None:
        await active.update(stage, post_id)


async def stop_heartbeat(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
