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
    from icecream import ic; ic("lookup", collection, key, "=", result) 
    if result is None:
        return ''
    return result
