from icecream import ic # DEBUG:

from hashlib import md5
from tempfile import NamedTemporaryFile
from pathlib import Path
from io import BytesIO
import logging
import copy
import os

import ffmpeg # https://github.com/kkroening/ffmpeg-python
from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from webvtt import WebVTT

from .models import Segment, DatasetVideo, Assignment, Worker
from .utils import secs_to_timestamp, timestamp_to_secs, load_subtitles
from .mturk import MTurk, make_aws_session, NoSessionError
from .storage import StoredFile


logger = logging.getLogger(__name__)



def cut_and_delocalize_video(dataset_video, credentials, location):
    session = make_aws_session(credentials) if credentials and location else None

    # cut_video
    cut_dataset_video(dataset_video, session, location)

    # save video to storage
    dataset_video.video.file.delocalize(session, location)
    dataset_video.video.save()
    if dataset_video.audio:
        dataset_video.audio.delocalize(session, location)
    if dataset_video.subtitles:
        dataset_video.subtitles.delocalize(session, location)
    # save dataset video to DB
    dataset_video.is_cut = True
    dataset_video.save()


def cut_video(video, audio, start, end, temp_mp4):
    opts = {}
    if end:
        opts['t'] = end - start

    if audio:
        out_map = ['0:v', '1:a']
    else:
        out_map = ['0']

    vstream = ffmpeg.input(
        video,
        ss=start,
        **opts,
    )
    if audio:
        astream = ffmpeg.input(
            audio,
            ss=start,
            **opts,
        )
        out_opts = {
            "c:v": "copy",
            "c:a": "aac",
        }
        ostream = ffmpeg.output(
            vstream['v:0'], astream['a:0'],
            temp_mp4.name,
            **out_opts,
        )
    else:
        ostream = ffmpeg.output(
            vstream,
            temp_mp4.name,
            c='copy',
        )
    ostream.run()

    file_path = Path(temp_mp4.name).relative_to(settings.MEDIA_ROOT)
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

def cut_dataset_video(dataset_video, session, location):
    temp_dir = default_storage.path('tmp')
    os.makedirs(temp_dir, exist_ok=True)
    dataset_video.segments.all().delete()
    with dataset_video.subtitles.local(session) as subtitles_path:
        subtitles = load_subtitles(subtitles_path)
    if dataset_video.cuts:
        for cut in dataset_video.cuts:
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
            video_path_ctx = dataset_video.video.file.local(session)
            audio_path_ctx = dataset_video.audio.local(session)
            with temp_mp4, temp_vtt, video_path_ctx as video_path, audio_path_ctx as audio_path:
                temp_mp4.close()
                temp_vtt.close()

                mp4_file = cut_video(video_path, audio_path, start, end, temp_mp4)
                seg_subtitles = cut_subtitles(subtitles, start, end, temp_vtt)
                video_file = StoredFile.save(mp4_file, "video_files", session, location, with_md5=True)
                md5sum = video_file.pop('md5')
                subs_file = StoredFile.save(seg_subtitles, "video_files", session, location)
                video_file.delocalize(session, location)
                subs_file.delocalize(session, location)

                segment = Segment.objects.create(
                    md5sum=md5sum,
                    dataset_video=dataset_video,
                    file=video_file,
                    start=start,
                    end=end,
                    subtitles=subs_file,
                )


def post_project_to_mturk(project, turk_credentials):
    messages = []
    is_started = True
    turk_hit_group_id = None
    try:
        mturk = MTurk()
        mturk.connect(turk_credentials)
        turk_hit_group_id = mturk.create_hits(project, messages)
    except Exception as x:
        ic(x)
        messages.append(['error', str(x)])
        is_started = False
    finally:
        turk_hit_group_id = { 'turk_hit_group_id': turk_hit_group_id } if turk_hit_group_id else {}
        type(project).objects.filter(pk=project.id).update(
            is_busy=False, is_started=is_started,
            messages=messages, **turk_hit_group_id,
        )


def get_assignments_from_mturk(project, turk_credentials):
    messages = []
    try:
        mturk = MTurk()
        mturk.connect(turk_credentials)
        tasks = project.tasks.exclude(turk_hit_id='').prefetch_related('assignments')
        for task in tasks:
            turk_assignments = mturk.get_assignments(task.turk_hit_id, project.questions)
            for turk_assignment_id, turk_assignment in turk_assignments.items():
                worker, _created = Worker.objects.get_or_create(turk_worker_id=turk_assignment['worker_id'])
                defaults = {
                    "worker": worker,
                    "is_approved": turk_assignment['is_approved'],
                    "result": turk_assignment['result'],
                }
                Assignment.objects.update_or_create(
                    task=task, turk_assignment_id=turk_assignment_id,
                    defaults=defaults,
                )
    except Exception as x:
        messages.append(['error', str(x)])
    finally:
        type(project).objects.filter(pk=project.id).update(
            is_busy=False, messages=messages,
        )


def vacuum():
    pass # TODO: implement
