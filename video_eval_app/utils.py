from icecream import ic # XXX: delete later

import csv
import codecs
from io import BytesIO, StringIO

from webvtt import WebVTT, Caption # https://pypi.org/project/webvtt-py/
from webvtt.models import Timestamp
from webvtt.srt import SRTCueBlock
from webvtt.sbv import SBVCueBlock


def secs_to_timestamp(secs):
    if secs is None:
        return None
    hours, secs = divmod(secs, 3600)
    mins, secs = divmod(secs, 60)
    secs, msecs = divmod(secs, 1)
    return str(Timestamp(int(hours), int(mins), int(secs), int(msecs * 1000)))

def timestamp_to_secs(ts):
    ts = Timestamp.from_string(ts)
    return ts.hours * 3600 + ts.minutes * 60 + ts.seconds + ts.milliseconds / 1000.0

def _detect_question_klass(question):
    question_type = question['type']
    if question_type == 'number':
        if all(type(question.get(prop, 0)) == int for prop in ["min", "max", "step"]):
            return int
        else:
            return float
    if 'options' not in question:
        return str
    klasses = set(type(option['value']) for option in question['options'])
    if len(klasses) == 1:
        return klasses.pop()
    return str # klass unknown

def _get_question_klasses(questions):
    return {
        question["id"]: _detect_question_klass(question)
        for question in questions
    }

def _convert_answer(value, klass):
    if not value:
        if value == '' and klass is str:
            return value
        if value is None:
            return None
    return klass(value)

def convert_answers(questions, request=None, turk_answers=None):
    question_klasses = _get_question_klasses(questions)
    result = {}
    for question in questions:
        question_id = question['id']
        question_klass = question_klasses[question_id]
        if question["type"] == 'checkbox':
            if request:
                values = request.POST.getlist(f'q-{question_id}')
            else:
                values = turk_answers.get(f'q-{question_id}').split('|')
            answer = [
                _convert_answer(value, question_klass)
                for value in values
            ]
        else:
            if request:
                value = request.POST.get(f'q-{question_id}')
            else:
                value = turk_answers.get(f'q-{question_id}')
            answer = _convert_answer(value, question_klass)
        result[question_id] = answer
    return result


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

def load_subtitles(subtitles_path=None, sub_contents=None):
    if subtitles_path is None:
        if sub_contents is None:
            return None
    else:
        with open(subtitles_path, "rb") as r:
            sub_contents = r.read()
        # strip BOM if present
        if sub_contents.startswith(codecs.BOM_UTF8):
            sub_contents = sub_contents[len(codecs.BOM_UTF8):]
    text_contents = sub_contents.decode()
    lines = text_contents.splitlines()
    if not lines:
        return None

    # try to detect format:
    maybe_vtt, *rest = lines[0].split(maxsplit=1)
    if maybe_vtt == "WEBVTT":
        format = 'vtt'
    elif SRTCueBlock.is_valid(lines):
        format = 'srt'
    elif SBVCueBlock.is_valid(lines):
        format = 'sbv'
    else:
        format = 'csv'
    if format == 'csv':
        subtitles = load_subtitles_from_csv(text_contents)
    else:
        subtitles = WebVTT.from_buffer(BytesIO(sub_contents), format=format)
    return subtitles
