from tempfile import NamedTemporaryFile
from pathlib import Path
from io import BytesIO, StringIO
import codecs
import csv
import logging
import copy

import ffmpeg # https://github.com/kkroening/ffmpeg-python
from webvtt import WebVTT, Caption # https://pypi.org/project/webvtt-py/
from webvtt.srt import SRTCueBlock
from webvtt.sbv import SBVCueBlock
from webvtt.vtt import WebVTTCueBlock
from django.conf import settings
from django.core.files import File

from .models import Segment, DatasetVideo
from .storage import md5
from .utils import secs_to_timestamp, timestamp_to_secs

logger = logging.getLogger(__name__)




def cut_video(video, audio, start, end, temp_mp4):
    opts = {}
    if end:
        opts['t'] = end - start

    if audio:
        out_map = ['0:v', '1:a']
    else:
        out_map = ['0']

    vstream = ffmpeg.input(
        video.path,
        ss=start,
        **opts,
    )
    if audio:
        astream = ffmpeg.input(
            audio.path,
            ss=start,
            **opts,
        )
        ostream = ffmpeg.output(
            vstream['v:0'], astream['a:0'],
            temp_mp4.name,
            c='copy',
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

def load_subtitles_from_csv(text_contents):
    sniffer = csv.Sniffer()
    headers = sniffer.has_header(text_contents)
    dialect = sniffer.sniff(text_contents)
    csv_reader = csv.reader(StringIO(text_contents), dialect)
    subs = WebVTT()
    if headers:
        _ = next(csv_reader)
    for row in csv_reader:
        if len(row) < 4:
            raise ValueError("Bad CSV file")
        caption = Caption(
            secs_to_timestamp(float(row[1])),
            secs_to_timestamp(float(row[2])),
            [row[3]],
        )
        subs.captions.append(caption)
    return subs

def load_subtitles(dataset_video):
    if dataset_video.subtitles is None:
        return None
    sub_contents = dataset_video.subtitles.read()
    # strip BOM if present
    if sub_contents.startswith(codecs.BOM_UTF8):
        sub_contents = sub_contents[len(codecs.BOM_UTF8):]
    text_contents = sub_contents.decode()
    lines = text_contents.splitlines()
    # try to detect format:
    if SRTCueBlock.is_valid(lines):
        format = 'srt'
    elif SBVCueBlock.is_valid(lines):
        format = 'sbv'
    elif WebVTTCueBlock.is_valid(lines):
        format = 'vtt'
    else:
        format = 'csv'
    if format == 'csv':
        subtitles = load_subtitles_from_csv(text_contents)
    else:
        subtitles = WebVTT.from_buffer(BytesIO(sub_contents), format=format)
    return subtitles

def cut_subtitles(all_subs, start, end, temp_vtt):
    if all_subs is None:
        return None
    subs = WebVTT()
    for orig_caption in all_subs.iter_slice(secs_to_timestamp(start), secs_to_timestamp(end)):
        caption = copy.copy(orig_caption)
        caption.start = orig_caption.start and str(secs_to_timestamp(timestamp_to_secs(orig_caption.start) - start))
        caption.end = orig_caption.end and str(secs_to_timestamp(timestamp_to_secs(orig_caption.end) - start))
        subs.captions.append(caption)
    file = File(file=StringIO(subs.content), name="dummy.vtt")
    return file

def cut_dataset_video(dataset_video):
    temp_dir = settings.MEDIA_ROOT / 'tmp'
    temp_dir.mkdir(parents=True, exist_ok=True)
    dataset_video.segments.all().delete()
    subtitles = load_subtitles(dataset_video)
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
            with temp_mp4, temp_vtt:
                temp_mp4.close()
                temp_vtt.close()

                mp4_file = cut_video(dataset_video.video.file, dataset_video.audio, start, end, temp_mp4)
                md5sum = md5(mp4_file)

                seg_subtitles = cut_subtitles(subtitles, start, end, temp_vtt)

                segment = Segment.objects.create(
                    dataset_video=dataset_video,
                    file=mp4_file,
                    md5sum=md5sum,
                    start=start,
                    end=end,
                    subtitles=seg_subtitles,
                )

    # mark as cut without triggering post_save
    DatasetVideo.objects.filter(pk=dataset_video.id).update(is_cut=True)


def vacuum():
    pass # TODO: implement
