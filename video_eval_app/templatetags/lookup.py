from django import template

register = template.Library()

@register.filter
def lookup(collection, key):
    result = None
    if isinstance(collection, dict):
        result = collection.get(key)
    elif isinstance(collection, list):
        try:
            result = collection[key]
        except IndexError:
            pass
    if result is None:
        return ''
    return result
