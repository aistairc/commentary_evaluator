import json

from django.conf import settings
from django.urls import resolve


class CredentialMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.user # preload user

        credentials = request.COOKIES.get(settings.CREDENTIALS_COOKIE_NAME)
        if credentials:
            request.credentials = json.loads(credentials)
        else:
            request.credentials = None

        return self.get_response(request)


class CurrentURLNameMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        url_name = resolve(request.path_info).url_name
        request.current_url_name = url_name
        return self.get_response(request)
