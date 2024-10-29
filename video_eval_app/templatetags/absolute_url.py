from django import template

register = template.Library()

@register.simple_tag(takes_context=True)
def absolute_url(context, url, *args, **kwargs):
    if request := context.get('request'):
        return request.build_absolute_uri(url)
    else:
        return 'http://EXAMPLE.COM/' + url.lstrip('/')
