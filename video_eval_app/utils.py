from webvtt.models import Timestamp


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
