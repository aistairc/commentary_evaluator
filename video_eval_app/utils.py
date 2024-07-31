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

