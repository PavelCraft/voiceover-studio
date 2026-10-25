from django.http import HttpResponse
from django.utils.translation import gettext as _

from studio.services.storage import StorageLimitError, ensure_storage_available


class StorageQuotaMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            if request.method in {"POST", "PUT", "PATCH"} and (request.content_type or "").startswith("multipart/form-data"):
                ensure_storage_available(int(request.META.get("CONTENT_LENGTH") or 0))
            return self.get_response(request)
        except StorageLimitError as error:
            return HttpResponse(_(str(error)), status=507, content_type="text/plain; charset=utf-8")

    def process_exception(self, request, exception):
        if isinstance(exception, StorageLimitError):
            return HttpResponse(_(str(exception)), status=507, content_type="text/plain; charset=utf-8")
        return None
