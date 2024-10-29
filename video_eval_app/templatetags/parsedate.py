from django import template
from datetime import datetime

register = template.Library()

@register.filter
def parsedate(datestring):
    return datetime.fromisoformat(datestring)
