"""Separate private Avito photographs, always outside the web checkout."""
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.deconstruct import deconstructible
from .storage import PrivateMediaStorage


@deconstructible
class AvitoImageStorage(PrivateMediaStorage):
    def __init__(self):
        checkout = Path(settings.BASE_DIR).resolve()
        root = Path(getattr(settings, 'AVITO_SERVICES_MEDIA_ROOT',
                            checkout.parent / 'private_avito_media')).resolve()
        if root == checkout or checkout in root.parents:
            raise ImproperlyConfigured('AVITO_SERVICES_MEDIA_ROOT должен находиться вне каталога веб-приложения.')
        super().__init__(location=str(root))
        self._file_permissions_mode = 0o600
        self._directory_permissions_mode = 0o700


avito_image_storage = AvitoImageStorage()
