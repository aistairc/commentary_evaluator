import json
from datetime import datetime

from django.conf import settings
from django.urls import resolve
from django.contrib import messages
from .json_schemata import credentials_schema, CredentialValidationError


class CredentialMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.user # preload user

        credentials_cookie = request.COOKIES.get(settings.CREDENTIALS_COOKIE_NAME)
        request.credentials = None
        clear_cookie = False
        
        if credentials_cookie:
            # Clear credentials if user is not authenticated (session expired/invalid)
            if not request.user.is_authenticated:
                clear_cookie = True
                # Don't show message here as user might be on login page
            else:
                try:
                    # Parse and validate credentials from cookie
                    credentials_data = json.loads(credentials_cookie)
                    validated_credentials = credentials_schema.validate(credentials_data)
                    
                    # Check if credentials are expired
                    if expiration_value := validated_credentials.get('Expiration'):
                        try:
                            # Handle both string and datetime objects
                            if isinstance(expiration_value, str):
                                expiration = datetime.fromisoformat(expiration_value)
                            else:
                                expiration = expiration_value
                            
                            if datetime.now(expiration.tzinfo) > expiration:
                                # Credentials are expired - clear them and add message
                                clear_cookie = True
                                messages.warning(request, 'AWS credentials have expired and have been cleared. Please upload fresh credentials.')
                            else:
                                # Valid and not expired
                                request.credentials = validated_credentials
                        except (ValueError, TypeError):
                            # Invalid expiration format - clear credentials
                            clear_cookie = True
                            messages.error(request, 'Invalid credential expiration format. Please re-upload credentials.')
                    else:
                        # No expiration (permanent credentials)
                        request.credentials = validated_credentials
                        
                except (json.JSONDecodeError, Exception):
                    # Invalid JSON or schema validation failed - clear cookie
                    clear_cookie = True
                    messages.error(request, 'Invalid credentials in session. Please re-upload credentials.')

        response = self.get_response(request)
        
        # Clear the credentials cookie if needed
        if clear_cookie:
            response.delete_cookie(settings.CREDENTIALS_COOKIE_NAME)
            
        return response


class CurrentURLNameMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        url_name = resolve(request.path_info).url_name
        request.current_url_name = url_name
        return self.get_response(request)
