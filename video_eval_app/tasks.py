from json import load
from django.contrib.auth.decorators import sync_to_async

from hashlib import md5
from tempfile import NamedTemporaryFile
from pathlib import Path
from io import BytesIO
import logging
import copy
import os
import asyncio
from contextlib import nullcontext

from ffmpeg.asyncio import FFmpeg # https://github.com/jonghwanhyeon/python-ffmpeg
from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from webvtt import WebVTT

from .models import Segment, DatasetVideo, Assignment, Worker, StoredFile
from django.contrib.auth.models import User
from .utils import secs_to_timestamp, timestamp_to_secs, load_subtitles
from .mturk import MTurk, make_aws_session


logger = logging.getLogger(__name__)



async def cut_video(video, audio, start, end, temp_mp4):
    t_opt = { "t": end - start } if end else {}
    out_map = ['0:v', '1:a'] if audio else ['0']

    ffmpeg = FFmpeg()
    ffmpeg = ffmpeg.input(
        video,
    )
    copy_opts = {
        'c:v': 'libx264'
    }
    if audio:
        ffmpeg = ffmpeg.input(
            audio,
        )
        copy_opts['c:a'] = 'aac'
        out_map = ['0:v', '1:a']
    else:
        out_map = ['0']
    ffmpeg = ffmpeg.output(
        temp_mp4.name,
        map=out_map,
        ss=start,
        **t_opt,
        **copy_opts
    )
    # import shlex; print(' '.join(shlex.quote(arg) for arg in ffmpeg.arguments))
    try:
        # Execute with configurable timeout
        await asyncio.wait_for(ffmpeg.execute(), timeout=settings.FFMPEG_TIMEOUT)
        
        # Verify output file was created and has reasonable size
        if not os.path.exists(temp_mp4.name):
            raise RuntimeError("FFmpeg completed but output file was not created")
        
        file_size = os.path.getsize(temp_mp4.name)
        if file_size == 0:
            raise RuntimeError("FFmpeg produced empty output file")
        
        logger.info(f"FFmpeg success: created {file_size} byte file")
        
    except asyncio.TimeoutError:
        logger.error(f"FFmpeg timeout after {settings.FFMPEG_TIMEOUT} seconds")
        raise RuntimeError(f"Video processing timeout after {settings.FFMPEG_TIMEOUT} seconds")
    
    except Exception as e:
        logger.error(f"FFmpeg failed: {str(e)}")
        # Clean up partial output
        if os.path.exists(temp_mp4.name):
            os.unlink(temp_mp4.name)
        raise RuntimeError(f"Video processing failed: {str(e)}")

    # file_path = Path(temp_mp4.name).relative_to(settings.MEDIA_ROOT)
    file = File(file=open(temp_mp4.name, 'rb'), name="dummy.mp4")
    return file


def cut_subtitles(all_subs, start, end, temp_vtt):
    if all_subs is None:
        return None
    subs = WebVTT()
    for orig_caption in all_subs.iter_slice(secs_to_timestamp(start), secs_to_timestamp(end)):
        caption = copy.copy(orig_caption)
        caption.start = orig_caption.start and str(secs_to_timestamp(timestamp_to_secs(orig_caption.start) - start))
        caption.end = orig_caption.end and str(secs_to_timestamp(timestamp_to_secs(orig_caption.end) - start))
        subs.captions.append(caption)
    file = File(file=BytesIO(subs.content.encode()), name="dummy.vtt")
    return file

async def cut_dataset_video(dataset_video, session, location):
    def load_dependents():
        dataset_video.video
        dataset_video.audio
        dataset_video.subtitles
    await sync_to_async(load_dependents)()
    temp_dir = default_storage.path('tmp')
    os.makedirs(temp_dir, exist_ok=True)
    await dataset_video.segments.all().adelete()
    subtitles_context = dataset_video.subtitles.local(session) if dataset_video.subtitles else nullcontext()
    subtitles_path_ctx = dataset_video.subtitles.local(session) if dataset_video.subtitles else nullcontext()
    async with subtitles_path_ctx as subtitles_path:
        subtitles = load_subtitles(subtitles_path)
    cuts = dataset_video.cuts or [[0]]

    # Get the owner from the source video file to inherit ownership
    source_owner = await User.objects.aget(id=dataset_video.video.created_by_id) if dataset_video.video.created_by_id else None
    for cut in cuts:
        start = cut[0]
        end = cut[1] if len(cut) > 1 else None

        temp_mp4 = NamedTemporaryFile(
            dir=temp_dir,
            suffix=".mp4",
            delete=True,
        )
        temp_vtt = NamedTemporaryFile(
            dir=temp_dir,
            suffix=".vtt",
            delete=True,
        )
        video_path_ctx = dataset_video.video.local(session)
        audio_path_ctx = dataset_video.audio.local(session) if dataset_video.audio else nullcontext()
        with temp_mp4, temp_vtt:
            temp_mp4.close()
            temp_vtt.close()

            async with video_path_ctx as video_path, audio_path_ctx as audio_path:
                mp4_file = await cut_video(video_path, audio_path, start, end, temp_mp4)
            seg_subtitles = cut_subtitles(subtitles, start, end, temp_vtt)
            video_file = await StoredFile.store(mp4_file, "video_files", session, location, created_by=source_owner)
            mp4_file.close()
            subs_file = await StoredFile.store(seg_subtitles, "subs_files", session, location, created_by=source_owner)
            if seg_subtitles:
                seg_subtitles.close()
            await video_file.delocalize(session, location)
            if subs_file:
                await subs_file.delocalize(session, location)

            await Segment.objects.acreate(
                dataset_video=dataset_video,
                video=video_file,
                start=start,
                end=end,
                subtitles=subs_file,
            )



async def cut_and_delocalize_video(dataset_video, session, location):
    # cut_video
    await cut_dataset_video(dataset_video, session, location)

    # save video to storage
    await dataset_video.video.delocalize(session, location)
    await dataset_video.video.asave()
    if dataset_video.audio:
        await dataset_video.audio.delocalize(session, location)
    if dataset_video.subtitles:
        await dataset_video.subtitles.delocalize(session, location)
    # save dataset video to DB
    dataset_video.is_cut = True
    await dataset_video.asave()


async def post_project_to_mturk(project, tasks, mturk):
    messages = []
    is_started = True
    turk_hit_group_id = None
    try:
        async with mturk.connect() as client:
            turk_hit_group_id = await mturk.create_hits(client, project, tasks, messages)
    # except Exception as x:
    except Exception as x:
        messages.append(['error', str(x)])
        is_started = False
    finally:
        await type(project).objects.filter(pk=project.id).aupdate(
            is_busy=False, is_started=is_started,
            messages=messages, turk_hit_group_id=turk_hit_group_id or '',
        )


async def get_assignments_from_mturk(project, turk_credentials):
    messages = []
    try:
        mturk = MTurk(turk_credentials)
        async with mturk.connect() as client:
            tasks = project.tasks.exclude(turk_hit_id='').prefetch_related('assignments')
            async for task in tasks:
                turk_assignments = await mturk.get_assignments(client, task.turk_hit_id, project.questions)
                for turk_assignment_id, turk_assignment in turk_assignments.items():
                    worker, _created = await Worker.objects.aget_or_create(worker_id=turk_assignment['worker_id'], service="MTurk")
                    defaults = {
                        "worker": worker,
                        "is_approved": turk_assignment['is_approved'],
                        "result": turk_assignment['result'],
                    }
                    assignment = await Assignment.objects.aupdate_or_create(
                        task=task, turk_assignment_id=turk_assignment_id,
                        defaults=defaults,
                    )
    except Exception as x:
        ic("error", str(x))
        messages.append(['error', str(x)])
    finally:
        await type(project).objects.filter(pk=project.id).aupdate(
            is_busy=False, messages=messages,
        )


def vacuum():
    pass # TODO: implement
