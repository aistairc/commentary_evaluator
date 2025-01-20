import asyncio
import atexit
import threading

from icecream import ic


async def _set_result(result_future, result):
    result_future.set_result(result)

async def _set_exception(result_future, exception):
    result_future.set_exception(exception)

class AsyncQueue:
    def __init__(self, num_workers=1):
        self.loop = asyncio.new_event_loop()
        self.queue = asyncio.Queue()
        self.thread = threading.Thread(target=self._start_loop, daemon=True)
        self.shutdown_event = threading.Event()

        self.thread.start()
        for ix in range(num_workers):
            asyncio.run_coroutine_threadsafe(self.worker(ix), self.loop)

    def _start_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def worker(self, ix):
        while not self.shutdown_event.is_set():
            try:
                func, args, kwargs, result_future = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                future_loop = result_future.get_loop()
                try:
                    result = await func(*args, **kwargs)
                    asyncio.run_coroutine_threadsafe(_set_result(result_future, result), future_loop)
                except Exception as x:
                    asyncio.run_coroutine_threadsafe(_set_exception(result_future, x), future_loop)
                finally:
                    self.queue.task_done()
            except asyncio.TimeoutError:
                pass # just try again

    async def __call__(self, func, *args, **kwargs):
        result_future = asyncio.Future()
        # await self.queue.put((func, args, kwargs, result_future))
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (func, args, kwargs, result_future))
        result = await result_future
        return result

    async def shutdown(self):
        self.shutdown_event.set()  # Signal shutdown
        self.loop.call_soon_threadsafe(self.loop.stop)  # Stop the event loop
        self.thread.join()  # Wait for the thread to finish


if __name__ == "__main__":
    async def main(fn, objs):
        async_queue = AsyncQueue()
        for obj in objs:
            await async_queue(fn, obj, None, None)

    import sys
    from pathlib import Path
    sys.path.append(str(Path(__file__).parent.parent))
    import os
    os.environ["DJANGO_SETTINGS_MODULE"] = "video_evaluation.settings"
    import django
    django.setup()

    from video_eval_app.models import *
    from .tasks import cut_and_delocalize_video
    videos = list(DatasetVideo.objects.filter(pk__gte=65).prefetch_related('video', 'audio', 'subtitles'))

    asyncio.run(main(cut_and_delocalize_video, videos))
