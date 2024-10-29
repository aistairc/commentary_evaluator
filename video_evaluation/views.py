from django.contrib.auth.views import LogoutView

from .settings import CREDENTIALS_COOKIE_NAME


class CookieDeletingLogoutView(LogoutView):
    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response.delete_cookie(CREDENTIALS_COOKIE_NAME)
        return response
