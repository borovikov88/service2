from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivateMediaStorage(FileSystemStorage):
    def __init__(self, location=None, base_url=None):
        super().__init__(
            location=location or settings.PRIVATE_MEDIA_ROOT,
            base_url=base_url,
        )

    def url(self, name):
        raise ValueError("Private files do not have public URLs.")


private_media_storage = PrivateMediaStorage()
